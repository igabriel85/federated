# Lint as: python3
# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for transforming_executor.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import asyncio

from absl.testing import absltest

import tensorflow as tf

from tensorflow_federated.python.core.api import computations
from tensorflow_federated.python.core.api import intrinsics
from tensorflow_federated.python.core.impl import computation_building_blocks
from tensorflow_federated.python.core.impl import executor_base
from tensorflow_federated.python.core.impl import transformations
from tensorflow_federated.python.core.impl import transforming_executor
from tensorflow_federated.python.core.impl import type_constructors


class FakeEx(executor_base.Executor):

  async def create_value(self, val, unused):
    return str(
        computation_building_blocks.ComputationBuildingBlock.from_proto(val))

  async def create_call(self, comp, arg=None):
    raise NotImplementedError

  async def create_tuple(self, elements):
    raise NotImplementedError

  async def create_selection(self, source, index=None, name=None):
    raise NotImplementedError


def _test_create_value(val, transform_fn):
  ex = transforming_executor.TransformingExecutor(transform_fn, FakeEx())
  return asyncio.get_event_loop().run_until_complete(ex.create_value(val))


@computations.federated_computation(tf.int32)
def _identity(x):
  return x


class TransformingExecutorTest(absltest.TestCase):

  def test_with_removal_of_identity_mapping(self):

    @computations.federated_computation(type_constructors.at_server(tf.int32))
    def comp(x):
      return intrinsics.federated_apply(_identity, x)

    def transformation_fn(x):
      x, _ = transformations.remove_mapped_or_applied_identity(x)
      return x

    self.assertEqual(
        _test_create_value(comp, transformation_fn),
        '(FEDERATED_arg -> FEDERATED_arg)')

  def test_with_inlining_of_blocks(self):

    @computations.federated_computation(type_constructors.at_server(tf.int32))
    def comp(x):
      return intrinsics.federated_zip([x, x])

    # TODO(b/134543154): Slide in something more powerful so that this test
    # doesn't break when the implementation changes; for now, this will do.
    def transformation_fn(x):
      x, _ = transformations.remove_mapped_or_applied_identity(x)
      x, _ = transformations.inline_block_locals(x)
      x, _ = transformations.replace_selection_from_tuple_with_element(x)
      return x

    self.assertIn('federated_zip_at_server(<FEDERATED_arg,FEDERATED_arg>)',
                  _test_create_value(comp, transformation_fn))


if __name__ == '__main__':
  tf.compat.v1.enable_v2_behavior()
  absltest.main()
