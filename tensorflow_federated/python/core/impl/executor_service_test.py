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
"""Tests for executor_service.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading

from absl.testing import absltest

import grpc
from grpc.framework.foundation import logging_pool
import portpicker

import tensorflow as tf

from tensorflow_federated.proto.v0 import executor_pb2
from tensorflow_federated.proto.v0 import executor_pb2_grpc
from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.core.api import computations
from tensorflow_federated.python.core.impl import eager_executor
from tensorflow_federated.python.core.impl import executor_base
from tensorflow_federated.python.core.impl import executor_service
from tensorflow_federated.python.core.impl import executor_service_utils
from tensorflow_federated.python.core.impl import executor_value_base


class TestEnv(object):
  """A test environment that consists of a single client and backend service."""

  def __init__(self, executor):
    port = portpicker.pick_unused_port()
    server_pool = logging_pool.pool(max_workers=1)
    self._server = grpc.server(server_pool)
    self._server.add_insecure_port('[::]:{}'.format(port))
    self._service = executor_service.ExecutorService(executor)
    executor_pb2_grpc.add_ExecutorServicer_to_server(self._service,
                                                     self._server)
    self._server.start()
    self._channel = grpc.insecure_channel('localhost:{}'.format(port))
    self._stub = executor_pb2_grpc.ExecutorStub(self._channel)

  def __del__(self):
    # TODO(b/134543154): Find some way of cleanly disposing of channels that is
    # consistent between Google-internal and OSS stacks.
    try:
      self._channel.close()
    except AttributeError:
      # The `.close()` method does not appear to be present in grpcio 1.8.6, so
      # we have to fall back on explicitly calling the destructor.
      del self._stub
      del self._channel
    self._server.stop(None)

  @property
  def stub(self):
    return self._stub

  def get_value(self, value_id):
    response = self._stub.Compute(
        executor_pb2.ComputeRequest(
            value_ref=executor_pb2.ValueRef(id=value_id)))
    py_typecheck.check_type(response, executor_pb2.ComputeResponse)
    value, _ = executor_service_utils.deserialize_value(response.value)
    return value


class ExecutorServiceTest(absltest.TestCase):

  def test_executor_service_slowly_create_tensor_value(self):

    class SlowExecutorValue(executor_value_base.ExecutorValue):

      def __init__(self, v, t):
        self._v = v
        self._t = t

      @property
      def type_signature(self):
        return self._t

      async def compute(self):
        return self._v

    class SlowExecutor(executor_base.Executor):

      def __init__(self):
        self.status = 'idle'
        self.busy = threading.Event()
        self.done = threading.Event()

      async def create_value(self, value, type_spec=None):
        self.status = 'busy'
        self.busy.set()
        self.done.wait()
        self.status = 'done'
        return SlowExecutorValue(value, type_spec)

      async def create_call(self, comp, arg=None):
        raise NotImplementedError

      async def create_tuple(self, elements):
        raise NotImplementedError

      async def create_selection(self, source, index=None, name=None):
        raise NotImplementedError

    ex = SlowExecutor()
    env = TestEnv(ex)
    self.assertEqual(ex.status, 'idle')
    value_proto, _ = executor_service_utils.serialize_value(10, tf.int32)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    ex.busy.wait()
    self.assertEqual(ex.status, 'busy')
    ex.done.set()
    value = env.get_value(response.value_ref.id)
    self.assertEqual(ex.status, 'done')
    self.assertEqual(value, 10)

  def test_executor_service_create_tensor_value(self):
    env = TestEnv(eager_executor.EagerExecutor())
    value_proto, _ = executor_service_utils.serialize_value(
        tf.constant(10.0).numpy(), tf.float32)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    value_id = str(response.value_ref.id)
    value = env.get_value(value_id)
    self.assertEqual(value, 10.0)
    del env

  def test_executor_service_create_no_arg_computation_value_and_call(self):
    env = TestEnv(eager_executor.EagerExecutor())

    @computations.tf_computation
    def comp():
      return tf.constant(10)

    value_proto, _ = executor_service_utils.serialize_value(comp)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    response = env.stub.CreateCall(
        executor_pb2.CreateCallRequest(function_ref=response.value_ref))
    self.assertIsInstance(response, executor_pb2.CreateCallResponse)
    value_id = str(response.value_ref.id)
    value = env.get_value(value_id)
    self.assertEqual(value, 10)
    del env

  def test_executor_service_create_one_arg_computation_value_and_call(self):
    env = TestEnv(eager_executor.EagerExecutor())

    @computations.tf_computation(tf.int32)
    def comp(x):
      return tf.add(x, 1)

    value_proto, _ = executor_service_utils.serialize_value(comp)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    comp_ref = response.value_ref

    value_proto, _ = executor_service_utils.serialize_value(10, tf.int32)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    arg_ref = response.value_ref

    response = env.stub.CreateCall(
        executor_pb2.CreateCallRequest(
            function_ref=comp_ref, argument_ref=arg_ref))
    self.assertIsInstance(response, executor_pb2.CreateCallResponse)
    value_id = str(response.value_ref.id)
    value = env.get_value(value_id)
    self.assertEqual(value, 11)
    del env

  def test_executor_service_create_and_select_from_tuple(self):
    env = TestEnv(eager_executor.EagerExecutor())

    value_proto, _ = executor_service_utils.serialize_value(10, tf.int32)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    ten_ref = response.value_ref
    self.assertEqual(env.get_value(ten_ref.id), 10)

    value_proto, _ = executor_service_utils.serialize_value(20, tf.int32)
    response = env.stub.CreateValue(
        executor_pb2.CreateValueRequest(value=value_proto))
    self.assertIsInstance(response, executor_pb2.CreateValueResponse)
    twenty_ref = response.value_ref
    self.assertEqual(env.get_value(twenty_ref.id), 20)

    response = env.stub.CreateTuple(
        executor_pb2.CreateTupleRequest(element=[
            executor_pb2.CreateTupleRequest.Element(
                name='a', value_ref=ten_ref),
            executor_pb2.CreateTupleRequest.Element(
                name='b', value_ref=twenty_ref)
        ]))
    self.assertIsInstance(response, executor_pb2.CreateTupleResponse)
    tuple_ref = response.value_ref
    self.assertEqual(str(env.get_value(tuple_ref.id)), '<a=10,b=20>')

    for arg_name, arg_val, result_val in [('name', 'a', 10), ('name', 'b', 20),
                                          ('index', 0, 10), ('index', 1, 20)]:
      response = env.stub.CreateSelection(
          executor_pb2.CreateSelectionRequest(
              source_ref=tuple_ref, **{arg_name: arg_val}))
      self.assertIsInstance(response, executor_pb2.CreateSelectionResponse)
      selection_ref = response.value_ref
      self.assertEqual(env.get_value(selection_ref.id), result_val)

    del env


if __name__ == '__main__':
  tf.compat.v1.enable_v2_behavior()
  absltest.main()
