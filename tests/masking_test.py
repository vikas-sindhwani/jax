# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial
from unittest import SkipTest

import numpy as onp
from absl.testing import absltest
from absl.testing import parameterized

from jax import test_util as jtu
from jax.interpreters.masking import ShapeError, shape_as_value, parse_spec
from jax import mask, vmap, jit, grad, shapecheck
from jax import lax
import jax.numpy as np

from jax.config import config
config.parse_flags_with_absl()


# These are 'manual' tests for masking and shape checking. The more exhaustive,
# more systematic tests should live in lax_test.py.

class MaskingTest(jtu.JaxTestCase):

  @parameterized.parameters([
      ['(m, n)', 'ShapeSpec(m, n)'],
      ['(m * n)', 'ShapeSpec(m n)'],
      ['m * n', 'ShapeSpec(m n)'],
      ['(m * n,)', 'ShapeSpec(m n)'],
      ['(3, m)', 'ShapeSpec(3, m)'],
      ['(3 * m)', 'ShapeSpec(3 m)'],
      ['m', 'ShapeSpec(m)'],
      ['', 'ShapeSpec()'],
      ['m + n', 'ShapeSpec(m + n)'],
      ['m + n * k', 'ShapeSpec(m + k n)'],
      ['m + 3 * k', 'ShapeSpec(3 k + m)'],
      ['', 'ShapeSpec()'],
      ['_', 'ShapeSpec(_)'],
  ])
  def test_shape_parsing(self, spec, ans):
    self.assertEqual(str(parse_spec(spec)), ans)

  def test_dot_shape_checking(self):
    @shapecheck(['(m, n)', 'n'], 'm')
    def matvec(A, b):
      return np.dot(A, b)

    def thunk():
      @shapecheck(['(m, n)', 'n'], 'm')
      def matvec(A, b):
        return np.dot(b, A)
    self.assertRaisesRegex(ShapeError, "", thunk)

  def test_flatten_shape_checking(self):
    @shapecheck(['(m, n)'], 'm * n')
    def flatten(x):
      return lax.reshape(x, (x.shape[0] * x.shape[1],))

  def test_concatenate_shape_checking(self):
    @shapecheck(['m', 'n', 'm'], '3*m + n')
    def cat(x, y, z):
      return lax.concatenate([x, y, x, z], 0)

    def thunk():
      @shapecheck(['m', 'n', 'm'], '3*m + n')
      def cat(x, y, z):
        return lax.concatenate([x, y, x], 0)
    self.assertRaisesRegex(ShapeError, "", thunk)

  def test_sum(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def padded_sum(x):
      return np.sum(x)

    ans = padded_sum([np.array([3, 1, 4, 1, 5])], dict(n=3))
    expected = 8
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = padded_sum([np.array([3, 1, 4, 1, 5])], dict(n=4))
    expected = 9
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_sum_vmap(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def padded_sum(x):
      return np.sum(x)

    ans = vmap(padded_sum)([np.ones((5, 10))], dict(n=np.arange(5)))
    expected = onp.array([0, 1, 2, 3, 4])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_add(self):
    @partial(mask, in_shapes=['n', 'n'], out_shape='n')
    def addvecs(x, y):
      return x + y

    x = np.array([3, 1, 4, 1, 5, 9])
    y = np.array([2, 6, 5, 3, 5, 8])
    ans = addvecs([x, y], dict(n=3))
    expected = onp.array([5, 7, 9])
    self.assertAllClose(ans[:3], expected, check_dtypes=False)

    thunk = lambda: addvecs([np.arange(5), np.arange(6)], dict(n=3))
    self.assertRaisesRegex(ShapeError, "", thunk)

  def test_scan(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def cumsum(arr):
      out, _ = lax.scan(lambda c, x: (c + x, ()), 0, arr)
      return out

    ans = cumsum([np.array([5, 2, 9, 1, 4])], dict(n=3))
    expected = 16
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_scan_vmap(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def cumsum(arr):
      out, _ = lax.scan(lambda c, x: (c + x, ()), 0, arr)
      return out

    ans = vmap(cumsum)([np.arange(6).reshape(2, 3)], dict(n=np.array([1, 2])))
    expected = onp.array([0, 7])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_scan_jit(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def cumsum(arr):
      out, _ = lax.scan(lambda c, x: (c + x, ()), 0, arr)
      return out

    @jit
    def jit_cumsum(args, shape_env):
      assert python_should_be_executing
      return cumsum(args, shape_env)

    python_should_be_executing = True
    ans = jit_cumsum([np.array([5, 2, 9, 1, 4])], dict(n=3))
    expected = 16
    self.assertAllClose(ans, expected, check_dtypes=False)

    python_should_be_executing = False
    ans = jit_cumsum([np.array([5, 2, 9, 1, 4])], dict(n=4))
    expected = 17
    self.assertAllClose(ans, expected, check_dtypes=False)

    python_should_be_executing = False
    ans = jit_cumsum([np.array([5, 2, 9, 1, 4])], dict(n=1))
    expected = 5
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_concatenate(self):
    @partial(mask, in_shapes=['n', 'm', 'n'], out_shape='m + 2 * n')
    def cat(x, y, z):
      return lax.concatenate([x, y, z], 0)

    ans = cat([np.array([1, 9]), np.array([2, 4, 9]), np.array([3, 9])],
              dict(n=1, m=2))
    expected = onp.array([1, 2, 4, 3])
    self.assertAllClose(ans[:4], expected, check_dtypes=False)

  def test_dot(self):
    @partial(mask, in_shapes=['(m, k)', '(k, n)'], out_shape='(m, n)')
    def dot(x, y):
      return lax.dot(x, y)

    x = onp.arange(6, dtype=onp.float32).reshape((2, 3))
    y = onp.arange(12, dtype=onp.float32).reshape((3, 4))
    ans = dot([x, y], dict(m=2, k=2, n=2))
    expected = onp.dot(x[:2, :2], y[:2, :2])
    self.assertAllClose(ans[:2, :2], expected, check_dtypes=False)

  def test_mean(self):
    @partial(mask, in_shapes=['n'], out_shape='')
    def padded_sum(x):
      return np.sum(x) / shape_as_value(x.shape)[0]

    ans = padded_sum([np.array([3, 1, 4, 1, 5])], dict(n=3))
    expected = 8 / 3
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_monomorphic(self):
    @partial(mask, in_shapes=['(_, n)'], out_shape='')
    def padded_sum(x):
      return np.sum(x)

    ans = padded_sum([np.array([[3, 4], [5, 6]])], dict(n=1))
    expected = 8
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_monomorphic2(self):
    @partial(mask, in_shapes=['(_, n)'], out_shape='n')
    def padded_sum(x):
      return np.sum(x, axis=0)

    ans = padded_sum([np.array([[3, 4], [5, 6]])], dict(n=2))
    expected = np.array([8, 10])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_monomorphic3(self):
    @partial(mask, in_shapes=['(_, n)'], out_shape='_')
    def padded_sum(x):
      return np.sum(x, axis=1)

    ans = padded_sum([np.array([[3, 4], [5, 6]])], dict(n=1))
    expected = np.array([3, 5])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_rnn(self):
    n = 3

    @partial(mask, in_shapes=['(_, _)', '(t, _)'], out_shape='_')
    def rnn(W, xs):
      def step(h, x):
        new_h = np.dot(W, h) + np.dot(W, x)
        return new_h, ()
      predicted, _ = lax.scan(step, np.zeros(n), xs)
      return predicted

    rng = onp.random.RandomState(0)
    W = onp.eye(n)
    xs = rng.randn(10, n)
    ans = rnn([W, xs], dict(t=4))
    expected = xs[:4].sum(0)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_rnn_grad(self):
    n = 3

    @partial(mask, in_shapes=['(_, _)', '(t, _)', '_'], out_shape='')
    def rnn(W, xs, target):
      def step(h, x):
        new_h = np.tanh(np.dot(W, h) + np.dot(W, x))
        return new_h, ()
      predicted, _ = lax.scan(step, np.zeros(n), xs)
      return np.sum((predicted - target)**2)

    rng = onp.random.RandomState(0)
    W = rng.randn(n, n)
    xs = rng.randn(10, n)
    y = rng.randn(n)

    ans = grad(lambda W: rnn([W, xs, y], dict(t=4)))(W)

    def rnn_reference(W, xs, target):
      h = np.zeros(n)
      for x in xs:
        h = np.tanh(np.dot(W, h) + np.dot(W, x))
      predicted = h
      return np.sum((predicted - target)**2)

    expected = grad(lambda W: rnn_reference(W, xs[:4], y))(W)

    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_ragged_batched_rnn(self):
    n = 3

    @partial(mask, in_shapes=('(_, _)', '(t, _)', '_'), out_shape='')
    def rnn(W, xs, target):
      def step(h, x):
        new_h = np.tanh(np.dot(W, h) + np.dot(W, x))
        return new_h, ()
      predicted, _ = lax.scan(step, np.zeros(n), xs)
      return np.sum((predicted - target)**2)

    rng = onp.random.RandomState(0)
    W = rng.randn(n, n)
    seqs = rng.randn(3, 10, n)
    ts = np.array([2, 5, 4])
    ys = rng.randn(3, n)

    ans = grad(lambda W: vmap(rnn, ((None, 0, 0), 0))((W, seqs, ys), dict(t=ts)).sum())(W)

    def rnn_reference(W, seqs, targets):
      total_loss = 0
      for xs, target in zip(seqs, targets):
        h = np.zeros(n)
        for x in xs:
          h = np.tanh(np.dot(W, h) + np.dot(W, x))
        predicted = h
        total_loss = total_loss + np.sum((predicted - target)**2)
      return total_loss

    seqs_ = [xs[:t] for xs, t in zip(seqs, ts)]
    expected = grad(lambda W: rnn_reference(W, seqs_, ys).sum())(W)

    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nesting(self):
    raise SkipTest("not yet implemented")

    @partial(mask, in_shapes=['n'], out_shape='')
    def padded_sum(x):
      return np.sum(x)

    batched_sum = vmap(padded_sum)

    @partial(mask, in_shapes=['(m, _)', 'm'], out_shape='')
    def fun(x, ns):
      return batched_sum([x], dict(n=ns)).sum()

    x = np.array([[3, 1, 4, 1],
                  [5, 9, 2, 6],
                  [5, 3, 5, 8]])
    ns = np.array([2, 3, 2])
    ans = fun([x, ns], dict(m=2))
    expected = 3+1 + 5+9+2
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_arange(self):
    raise SkipTest("not yet implemented")

    @partial(mask, in_shapes=['n'], out_shape='n')
    def padded_add(x):
      return x + lax.iota(x.shape[0])

    ans = padded_add([np.array([3, 1, 4, 1, 5])], dict(n=3))
    expected = onp.array([3, 2, 6])
    self.assertAllClose(ans[:3], expected, check_dtypes=False)


if __name__ == '__main__':
  absltest.main()
