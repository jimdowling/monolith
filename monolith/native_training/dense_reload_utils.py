# Copyright 2022 ByteDance and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import tensorflow as tf
from absl import logging, flags
import numpy as np
from collections import defaultdict
from typing import Dict, List, Optional, Union, Set, Iterator

from tensorflow.python.training.saver import Saver
from tensorflow.python.client.session import Session
from tensorflow.python.training.py_checkpoint_reader import NewCheckpointReader, CheckpointReader
from monolith.native_training.basic_restore_hook import CheckpointRestorerListener
from monolith.native_training.model_export.export_context import is_exporting


CUSTOM_RESTORE_OP = 'custom_restore_op'
CustomRestoreListenerKey = 'CustomRestoreListener'
# [TODO](fitz) this may not cover all situation
PAT = re.compile(r"^.+/part_(\d+)(/.*)?$")
DensePat = re.compile(r'''(^.*)(dense(?:_\d+)?)/(bias|kernel|trainable_kernel_norm)(.*)''')
DedupPat = re.compile(r'''.*/(dense(?:_\d+)?)/.+''')
FLAGS = flags.FLAGS

# for those name cannot convert auto, ue use re-express to convert them
_NameMapping = {
  re.compile('c_dot/mlp(.*)?$'): 'c_dot/compress_tower{}',
  re.compile('^dcn/kernel_(\d+)/trainable_norm(.*)?$'): 'kernel_{}_trainable_norm{}',
  re.compile('^dcn/kernel_(\d+)(.*)?$'): 'kernel_{}{}',
  re.compile('^dcn/bias_(\d+)/trainable_norm(.*)?$'): 'bias_{}_trainable_norm{}',
  re.compile('^dcn/bias_(\d+)(.*)?$'): 'bias_{}{}',
}


def add_mapping_rules(rules: Dict[str, str]):
  global _NameMapping
  _NameMapping.update({
    re.compile(pat): fmt
    for pat, fmt in rules.items()
  })


def node_name(name: str):
  name = name.strip().rstrip('/')
  if name.startswith('^'):
    name = name[1:]
  if ':' in name:
    frist, second = name.rsplit(':', 1)
    if second.isdigit():
      name = frist
  return name


def get_new_name(name: str):
  selected = []
  name = node_name(name)
  for term in name.split('/'):
    if term not in selected:
      selected.append(term)
  return '/'.join(selected)


def get_guess_name(name: str):
  for pat, fmt in _NameMapping.items():
    matched = pat.match(name)
    if matched:
      print(pat, matched.groups())
      guess_name = fmt.format(*matched.groups())
      return guess_name
  return name


def update_var_name_mapping_for_dense(var_name_mapping: Dict[str, str]) -> Dict[str, str]:
  dense_layers = {}
  for name, origin in var_name_mapping.items():
    matched = DensePat.match(name)
    if matched:
      prefix = matched.group(1).rstrip('/')
      dense_name = matched.group(2)
      dense_local_var = matched.group(3)
      surfix = matched.group(4).lstrip('/')
      if dense_name in dense_layers:
        if dense_local_var == 'bias':
          prefix = get_guess_name(prefix)
          dense_layers[dense_name]['bias_prefix'] = prefix
        dense_layers[dense_name]['dense_local_var'].append((prefix, dense_name, dense_local_var, surfix, origin))
      else:
        if dense_local_var == 'bias':
          bias_prefix = get_guess_name(prefix)
          prefix = bias_prefix
        else:
          bias_prefix = ''
        dense_layers[dense_name] = {
          'bias_prefix': bias_prefix,
          'dense_local_var': [(prefix, dense_name, dense_local_var, surfix, origin)]
        }

  dense_layers_refactor = {}
  for dense_name, layers_vars in dense_layers.items():
    bias_prefix = layers_vars['bias_prefix']
    if bias_prefix in dense_layers_refactor:
      prefix = dense_layers_refactor[bias_prefix]
      prefix[dense_name] = layers_vars['dense_local_var']
    else:
      dense_layers_refactor[bias_prefix] = {
        dense_name: layers_vars['dense_local_var']
      }

  for bias_prefix, layers_vars in dense_layers_refactor.items():
    dense_names = list(layers_vars)
    if len(dense_names) > 1:
      dense_names.sort(key=lambda x: 0 if x == 'dense' else int(x.split('_')[-1]))
    for i, dense_name in enumerate(dense_names):
      new_dense_name = f'dense_{i}'
      for var_terms in layers_vars[dense_name]:
        prefix, local_var_name, origin = var_terms[0], var_terms[2], var_terms[-1]
        if prefix == '' or bias_prefix.endswith(prefix):
          new_name = '/'.join([bias_prefix, new_dense_name, local_var_name, var_terms[3]]).rstrip('/')
          if local_var_name == 'bias':
            var_name_mapping[new_name] = origin
          else:
            if new_name not in var_name_mapping:
              var_name_mapping[new_name] = origin

  # note: this may introduce problem, we deal with it at calc_feed_dict
  for dense_name, layers_vars in dense_layers.items():
    bias_prefix = layers_vars['bias_prefix']
    for var_terms in layers_vars['dense_local_var']:
      prefix, origin = var_terms[0], var_terms[-1]
      if prefix == '' or bias_prefix.endswith(prefix):
        new_name = '/'.join([bias_prefix] + list(var_terms[1:-1])).rstrip('/')
        if new_name not in var_name_mapping:
          var_name_mapping[new_name] = origin


class CustomRestoreListener(CheckpointRestorerListener):

  def __init__(self, 
               alias_map: Dict[str, str] = None, 
               clear_nn: bool = False, 
               continue_training: bool = False,
               model_dir: str = None,
               enable_alias_map_auto_gen: bool = None):
    self._alias_map = alias_map
    self._clear_nn = clear_nn
    self._continue_training = continue_training
    self.model_dir = model_dir
    self.ckpt_name = None
    self.enable_alias_map_auto_gen = True if enable_alias_map_auto_gen is None else enable_alias_map_auto_gen

  def begin(self):
    logging.info('CustomRestoreListener begin ...')
    if is_exporting():
      return

    checkpoint_state = None
    try:
      checkpoint_state = tf.train.get_checkpoint_state(checkpoint_dir=self.model_dir)
      self.ckpt_name = checkpoint_state.model_checkpoint_path
    except Exception as e:
      return
    if checkpoint_state is None:
      return

    graph: tf.Graph = tf.compat.v1.get_default_graph()
    variables = graph.get_collection('variables')

    if self._clear_nn:
      assert self._alias_map is None
      if self.model_dir:
        flag_file = os.path.join(self.model_dir, 'clear_nn')
        if tf.io.gfile.exists(flag_file):
          logging.info(f'the clear nn flag_file exists, skip clear, {flag_file}')
          return
      
      init_op = tf.compat.v1.global_variables_initializer()
      setattr(init_op, 'model_dir', self.model_dir)
      if self._continue_training:
        gs_var = tf.compat.v1.train.get_or_create_global_step(graph=graph)
        ph = tf.compat.v1.placeholder(dtype=gs_var.dtype, shape=gs_var.shape, name="global_step_ph")
        update_gs_op = gs_var.assign(value=ph)
        graph.add_to_collection(CUSTOM_RESTORE_OP, ([init_op, update_gs_op], [ph], None))
      else:
        graph.add_to_collection(CUSTOM_RESTORE_OP, ([init_op], [None], None))
    elif self._need_build_custom_init_graph(variables):
      assign_ops, placeholders = [], []
      for variable in variables:
        # [TODO](fitz) usually after getting variables from collection,
        # the variable name is tensor like, with the surfix ':0', add test to ensure it
        var_name = node_name(variable.name)
        ph = tf.compat.v1.placeholder(dtype=variable.dtype, shape=variable.shape, name=var_name)
        # (fitz) since tf name scope mechanism, '_\d' may add as a suffix, 
        # as a result we record the origin_name variable name
        ph.origin_name = var_name
        assign_op = variable.assign(value=ph)
        assign_ops.append(assign_op)
        placeholders.append(ph)

      init_op = tf.group(assign_ops)
      graph.add_to_collection(CUSTOM_RESTORE_OP, ([init_op], placeholders, self._alias_map))
    else:
      logging.info("nothing to do in CustomRestoreListener")

  def _need_build_custom_init_graph(self, variables: List[tf.Variable]) -> bool:
    assert self._clear_nn == False
    # for compat, this may not cover all satuation
    if not self._alias_map and self.enable_alias_map_auto_gen:
      # 1) load variable name from ckpt
      ckpt: CheckpointReader = NewCheckpointReader(self.ckpt_name)
      all_old_var_names = set(ckpt.get_variable_to_dtype_map().keys())
      # 2) check if need alias reload, if we can find all variables in ckpt, no alias reload required
      cnt = 0
      pat = re.compile(r"/part_\d+")
      for variable in variables:
        expected_saved_varibale_name = node_name(''.join(pat.split(variable.name)))
        if expected_saved_varibale_name in all_old_var_names:
          cnt += 1
      if len(variables) == cnt:
        logging.info("The ckpt is compatable, no need alias reload")
        return False

      logging.info("The ckpt is incompatable, begin to generate alias reload automatical ...")

      # 3) try to convert old variable name to new one
      var_name_mapping = {}
      for name in all_old_var_names:
        var_name_mapping[get_new_name(name)] = name
      logging.info(f'var_name_mapping = {var_name_mapping}')
      update_var_name_mapping_for_dense(var_name_mapping)

      # 4) generate alias_map
      alias_map = {}
      for variable in variables:
        expected_saved_varibale_name = node_name(''.join(pat.split(variable.name)))
        var_name = node_name(variable.name)
        alias_map[var_name] = var_name_mapping.get(expected_saved_varibale_name)

      # 5) check whether alias_map is validate
      none_values = {name for name, value in alias_map.items() if value is None}
      for name in none_values:
        expected_saved_varibale_name = node_name(''.join(pat.split(name)))
        guess_name = get_guess_name(expected_saved_varibale_name)
        if guess_name in var_name_mapping:
          alias_map[name] = var_name_mapping[guess_name]
        else:
          logging.warning('The ckpt is incompatable, but cannot alias reload automatical, pls spectify an alias_map')
          logging.info(f'alias_map = {alias_map}')
          return False

      logging.info(f"The ckpt is incompatable, begin to generate alias reload automatical done!")
      # 6) assign alias_map
      self._alias_map = alias_map

    if self._alias_map:
      new_names_alias_map = set(self._alias_map.values())
      new_names_from_var = {node_name(variable.name) for variable in variables}
      return len(new_names_from_var - new_names_alias_map) > 0
    else:
      return False

  @classmethod
  def get(cls):
    return cls.__instance


def infer_variable_name(names: List[str]) -> Set[str]:
  new_names = set()
  pat = re.compile(r'/part_\d+')

  for name in names:
    items = pat.split(name)
    if len(items) == 1:
      new_names.add(items[0])
    else:
      new_names.add(''.join(items))
  
  return new_names


def calc_feed_dict(ckpt: CheckpointReader, alias_map: Dict[str, str], placeholders: list) -> Dict[str, np.ndarray]:
  all_old_var_names = set(ckpt.get_variable_to_dtype_map().keys())
  reversed_alias_map = defaultdict(list)
  for new_name, old_name in alias_map.items():
    reversed_alias_map[old_name].append(new_name)

  all_required_new_names = set(alias_map.keys())
  # because tf will merge partitioned variable when saving
  # we need to infer_variable_name by remove part_xx form variable name
  all_new_var_names = infer_variable_name(all_required_new_names)
  if len(all_new_var_names - all_old_var_names) == 0:
    logging.info('no need to use alias_map to restore ...')
    return None
  else:
    logging.info(f'need restore form alias_map: {all_required_new_names - all_old_var_names}')

  ph_dict = {}
  for ph in placeholders:
    if hasattr(ph, 'origin_name'):
      new_var_name = ph.origin_name
    else:
      raise Exception(f'Cannot get origin_name of {ph}')
    ph_dict[new_var_name] = ph

  result = {}
  for old_name, new_name_list in reversed_alias_map.items():
    if len(new_name_list) == 1:
      new_name = new_name_list[0]
      result[ph_dict[new_name]] = ckpt.get_tensor(old_name)
    else:
      # this branch is for partitioned variables
      old_tensor = ckpt.get_tensor(old_name)

      # deal with problem maybe introduced by update_var_name_mapping_for_dense
      new_groups = defaultdict(list)
      for new_name in new_name_list:
        matched = DedupPat.match(new_name)
        if matched:
          key = matched.group(1)
          new_groups[key].append(new_name)

      if len(new_groups) > 1:
        denses = sorted(new_groups, key=lambda x: 0 if x == 'dense' else int(x.split('_')[-1]))
        new_name_list = new_groups[denses[0]]

      if len(new_name_list) == 1:
        new_name = new_name_list[0]
        result[ph_dict[new_name]] = old_tensor
        continue

      # sort the partitioned sub variable by partition index
      # .+/part_xx/.*, we extract the last part_xx, and sort accrodingly
      new_name_list = sorted(new_name_list, key=lambda x: int(PAT.match(x).group(1)))

      # get the first dim for placeholder as splits
      splits = [ph_dict[name].shape[0] for name in new_name_list]

      # construct indices_or_sections for numpy.split function
      indices_or_sections = [0] * (len(splits) - 1)
      for i, val in enumerate(splits):
        if i == 0:
          indices_or_sections[i] = val
        elif i == len(splits) - 1:
          break
        else:
          indices_or_sections[i] = indices_or_sections[i-1] + val

      # split old_tensor into partition, 
      sub_tensors = np.split(old_tensor, indices_or_sections, axis=0)
      for name, tensor in zip(new_name_list, sub_tensors):
        result[ph_dict[name]] = tensor

  return result
