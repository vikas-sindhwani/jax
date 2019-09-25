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

import collections
import functools
from functools import partial
import itertools
from unittest import skip, SkipTest

from absl.testing import absltest
from absl.testing import parameterized

import numpy as onp
import numpy.random as npr
import six

from jax import api
from jax import core
from jax import lax
from jax import test_util as jtu
from jax import lax_reference
from jax.test_util import check_grads
from jax.interpreters import xla
from jax.lib import xla_bridge
from jax.lib import xla_client

from jax.config import config
config.parse_flags_with_absl()
FLAGS = config.FLAGS


def num_float_bits(dtype):
  return onp.finfo(xla_bridge.canonicalize_dtype(dtype)).bits


### lax tests

# For standard unops and binops, we can generate a large number of tests on
# arguments of appropriate shapes and dtypes using the following table.

float_dtypes = [onp.float32, onp.float64]
complex_dtypes = [onp.complex64, onp.complex128]
inexact_dtypes = float_dtypes + complex_dtypes
int_dtypes = [onp.int32, onp.int64]
bool_dtypes = [onp.bool_]
default_dtypes = float_dtypes + int_dtypes
all_dtypes = float_dtypes + complex_dtypes + int_dtypes + bool_dtypes

compatible_shapes = [[(3,)], [(3, 4), (3, 1), (1, 4)], [(2, 3, 4), (2, 1, 4)]]

OpRecord = collections.namedtuple("OpRecord",
                                  ["op", "nargs", "dtypes", "rng", "tol"])


def op_record(op, nargs, dtypes, rng, tol=1e-5):
  return OpRecord(op, nargs, dtypes, rng, tol)

LAX_OPS = [
    op_record(lax.neg, 1, default_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.sign, 1, default_dtypes, jtu.rand_small()),
    op_record(lax.floor, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.ceil, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.round, 1, float_dtypes, jtu.rand_default()),

    op_record(lax.is_finite, 1, float_dtypes, jtu.rand_small()),

    op_record(lax.exp, 1, float_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.expm1, 1, float_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.log, 1, float_dtypes + complex_dtypes, jtu.rand_positive()),
    op_record(lax.log1p, 1, float_dtypes + complex_dtypes, jtu.rand_positive()),
    op_record(lax.tanh, 1, float_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.sin, 1, float_dtypes + complex_dtypes, jtu.rand_default()),
    op_record(lax.cos, 1, float_dtypes + complex_dtypes, jtu.rand_default()),
    op_record(lax.atan2, 2, float_dtypes, jtu.rand_default()),

    op_record(lax.sqrt, 1, float_dtypes + complex_dtypes, jtu.rand_positive()),
    op_record(lax.rsqrt, 1, float_dtypes + complex_dtypes, jtu.rand_positive()),
    op_record(lax.square, 1, float_dtypes + complex_dtypes, jtu.rand_default()),
    op_record(lax.reciprocal, 1, float_dtypes + complex_dtypes, jtu.rand_positive()),
    op_record(lax.tan, 1, float_dtypes, jtu.rand_default()),
    op_record(lax.asin, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.acos, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.atan, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.sinh, 1, float_dtypes + complex_dtypes, jtu.rand_default()),
    op_record(lax.cosh, 1, float_dtypes + complex_dtypes, jtu.rand_default()),

    op_record(lax.lgamma, 1, float_dtypes, jtu.rand_positive()),
    op_record(lax.digamma, 1, float_dtypes, jtu.rand_positive()),
    op_record(lax.erf, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.erfc, 1, float_dtypes, jtu.rand_small()),
    op_record(lax.erf_inv, 1, float_dtypes, jtu.rand_small(), tol=1e-2),

    op_record(lax.real, 1, complex_dtypes, jtu.rand_default()),
    op_record(lax.imag, 1, complex_dtypes, jtu.rand_default()),
    op_record(lax.complex, 2, [onp.float32], jtu.rand_default()),
    op_record(lax.conj, 1, [onp.float32] + complex_dtypes, jtu.rand_default()),
    op_record(lax.abs, 1, default_dtypes + complex_dtypes, jtu.rand_default()),
    op_record(lax.pow, 2, float_dtypes + complex_dtypes, jtu.rand_positive()),

    op_record(lax.bitwise_and, 2, bool_dtypes, jtu.rand_small()),
    op_record(lax.bitwise_not, 1, bool_dtypes, jtu.rand_small()),
    op_record(lax.bitwise_or, 2, bool_dtypes, jtu.rand_small()),
    op_record(lax.bitwise_xor, 2, bool_dtypes, jtu.rand_small()),

    op_record(lax.add, 2, default_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.sub, 2, default_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.mul, 2, default_dtypes + complex_dtypes, jtu.rand_small()),
    op_record(lax.div, 2, default_dtypes + complex_dtypes, jtu.rand_nonzero()),
    op_record(lax.rem, 2, default_dtypes, jtu.rand_nonzero()),

    op_record(lax.max, 2, all_dtypes, jtu.rand_small()),
    op_record(lax.min, 2, all_dtypes, jtu.rand_small()),

    op_record(lax.eq, 2, all_dtypes, jtu.rand_some_equal()),
    op_record(lax.ne, 2, all_dtypes, jtu.rand_small()),
    op_record(lax.ge, 2, default_dtypes, jtu.rand_small()),
    op_record(lax.gt, 2, default_dtypes, jtu.rand_small()),
    op_record(lax.le, 2, default_dtypes, jtu.rand_small()),
    op_record(lax.lt, 2, default_dtypes, jtu.rand_small()),
]

CombosWithReplacement = itertools.combinations_with_replacement


class LaxTest(jtu.JaxTestCase):
  """Numerical tests for LAX operations."""

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": jtu.format_test_name_suffix(
            rec.op.__name__, shapes, itertools.repeat(dtype)),
         "op": rec.op, "rng": rec.rng, "shapes": shapes, "dtype": dtype}
        for shape_group in compatible_shapes
        for shapes in CombosWithReplacement(shape_group, rec.nargs)
        for dtype in rec.dtypes)
      for rec in LAX_OPS))
  def testOp(self, op, rng, shapes, dtype):
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": jtu.format_test_name_suffix(
            rec.op.__name__, shapes, itertools.repeat(dtype)),
         "op": rec.op, "rng": rec.rng, "shapes": shapes, "dtype": dtype,
         "tol": rec.tol}
        for shape_group in compatible_shapes
        for shapes in CombosWithReplacement(shape_group, rec.nargs)
        for dtype in rec.dtypes)
      for rec in LAX_OPS))
  def testOpAgainstNumpy(self, op, rng, shapes, dtype, tol):
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    numpy_op = getattr(lax_reference, op.__name__)
    self._CheckAgainstNumpy(op, numpy_op, args_maker, tol=tol)

  # TODO test shift_left, shift_right_arithmetic, shift_right_logical

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}".format(
          from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for rng in [jtu.rand_default()]))
  def testConvertElementType(self, from_dtype, to_dtype, rng):
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.convert_element_type(x, to_dtype)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for rng in [jtu.rand_default()]))
  def testConvertElementTypeAgainstNumpy(self, from_dtype, to_dtype, rng):
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.convert_element_type(x, to_dtype)
    numpy_op = lambda x: lax_reference.convert_element_type(x, to_dtype)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for rng in [jtu.rand_default()]))
  def testBitcastConvertType(self, from_dtype, to_dtype, rng):
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}"
       .format(from_dtype, to_dtype),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for rng in [jtu.rand_default()]))
  def testBitcastConvertTypeAgainstNumpy(self, from_dtype, to_dtype, rng):
    args_maker = lambda: [rng((2, 3), from_dtype)]
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    numpy_op = lambda x: lax_reference.bitcast_convert_type(x, to_dtype)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}".format(
          jtu.format_shape_dtype_string(min_shape, dtype),
          jtu.format_shape_dtype_string(operand_shape, dtype),
          jtu.format_shape_dtype_string(max_shape, dtype)),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype, "rng": rng}
      for min_shape, operand_shape, max_shape in [
          [(), (2, 3), ()],
          [(2, 3), (2, 3), ()],
          [(), (2, 3), (2, 3)],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testClamp(self, min_shape, operand_shape, max_shape, dtype, rng):
    shapes = [min_shape, operand_shape, max_shape]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    self._CompileAndCheck(lax.clamp, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}".format(
          jtu.format_shape_dtype_string(min_shape, dtype),
          jtu.format_shape_dtype_string(operand_shape, dtype),
          jtu.format_shape_dtype_string(max_shape, dtype)),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype, "rng": rng}
      for min_shape, operand_shape, max_shape in [
          [(), (2, 3), ()],
          [(2, 3), (2, 3), ()],
          [(), (2, 3), (2, 3)],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testClampAgainstNumpy(self, min_shape, operand_shape, max_shape, dtype,
                            rng):
    shapes = [min_shape, operand_shape, max_shape]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    self._CheckAgainstNumpy(lax.clamp, lax_reference.clamp, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dim={}_baseshape=[{}]_dtype={}_narrs={}".format(
          dim, ",".join(str(d) for d in base_shape), onp.dtype(dtype).name,
          num_arrs),
       "dim": dim, "base_shape": base_shape, "dtype": dtype,
       "num_arrs": num_arrs, "rng": rng}
      for num_arrs in [3]
      for dtype in default_dtypes
      for base_shape in [(4,), (3, 4), (2, 3, 4)]
      for dim in range(len(base_shape))
      for rng in [jtu.rand_default()]))
  def testConcatenate(self, dim, base_shape, dtype, num_arrs, rng):
    shapes = [base_shape[:dim] + (size,) + base_shape[dim+1:]
              for size, _ in zip(itertools.cycle([3, 1, 4]), range(num_arrs))]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = lambda *args: lax.concatenate(args, dim)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dim={}_baseshape=[{}]_dtype={}_narrs={}".format(
          dim, ",".join(str(d) for d in base_shape), onp.dtype(dtype).name,
          num_arrs),
       "dim": dim, "base_shape": base_shape, "dtype": dtype,
       "num_arrs": num_arrs, "rng": rng}
      for num_arrs in [3]
      for dtype in default_dtypes
      for base_shape in [(4,), (3, 4), (2, 3, 4)]
      for dim in range(len(base_shape))
      for rng in [jtu.rand_default()]))
  def testConcatenateAgainstNumpy(self, dim, base_shape, dtype, num_arrs, rng):
    shapes = [base_shape[:dim] + (size,) + base_shape[dim+1:]
              for size, _ in zip(itertools.cycle([3, 1, 4]), range(num_arrs))]
    args_maker = lambda: [rng(shape, dtype) for shape in shapes]
    op = lambda *args: lax.concatenate(args, dim)
    numpy_op = lambda *args: lax_reference.concatenate(args, dim)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in ["VALID", "SAME"]
      for rng in [jtu.rand_small()]))
  def testConv(self, lhs_shape, rhs_shape, dtype, strides, padding, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv(lhs, rhs, strides, padding)

    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in ["VALID", "SAME"]
      for rng in [jtu.rand_small()]))
  def testConvAgainstNumpy(self, lhs_shape, rhs_shape, dtype, strides, padding,
                           rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    op = lambda lhs, rhs: lax.conv(lhs, rhs, strides, padding)
    numpy_op = lambda lhs, rhs: lax_reference.conv(lhs, rhs, strides, padding)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([1, 2, 3], repeat=3)]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in [((0, 0), (0, 0)), ((1, 2), (2, 0))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2), (2, 2)], repeat=2)
      for rng in [jtu.rand_small()]))
  def testConvWithGeneralPadding(self, lhs_shape, rhs_shape, dtype, strides,
                                 padding, lhs_dilation, rhs_dilation, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation)

    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([1, 2, 3], repeat=3)]
      for dtype in [onp.float32] for strides in [(1, 1), (1, 2), (2, 1)]
      for padding in [((0, 0), (0, 0)), ((1, 2), (2, 0))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2), (2, 2)], repeat=2)
      for rng in [jtu.rand_small()]))
  def DISABLED_testConvWithGeneralPaddingAgainstNumpy(
      self, lhs_shape, rhs_shape, dtype, strides, padding, lhs_dilation,
      rhs_dilation, rng):
    # TODO(mattjj): make this test pass
    raise SkipTest("this test is incomplete")
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation)

    def numpy_fun(lhs, rhs):
      return lax_reference.conv_with_general_padding(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation)

    self._CheckAgainstNumpy(fun, numpy_fun, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       "_lhs_dilation={}_rhs_dilation={}"
       "_dims={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           strides, padding, lhs_dilation, rhs_dilation,
           ",".join(dim_nums)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dilation": lhs_dilation,
       "rhs_dilation": rhs_dilation, "dimension_numbers": dim_nums,
       "perms": perms, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((b, i, 9, 10), (j, i, 4, 5))
          for b, i, j in itertools.product([2, 3], repeat=3)]
      for dtype in float_dtypes for strides in [(1, 1), (2, 1)]
      for padding in [((1, 2), (2, 0))]
      for lhs_dilation, rhs_dilation in itertools.product(
          [(1, 1), (1, 2)], repeat=2)
      for rng in [jtu.rand_small()]
      for dim_nums, perms in [
        (("NCHW", "OIHW", "NCHW"), ([0, 1, 2, 3], [0, 1, 2, 3])),
        (("NHWC", "HWIO", "NHWC"), ([0, 2, 3, 1], [2, 3, 1, 0])),
        (("NCHW", "HWIO", "NHWC"), ([0, 1, 2, 3], [2, 3, 1, 0])),
      ]))
  def testConvGeneralDilated(self, lhs_shape, rhs_shape, dtype, strides,
                             padding, lhs_dilation, rhs_dilation,
                             dimension_numbers, perms, rng):
    lhs_perm, rhs_perm = perms  # permute to compatible shapes

    def args_maker():
      return [lax.transpose(rng(lhs_shape, dtype), lhs_perm),
              lax.transpose(rng(rhs_shape, dtype), rhs_perm)]

    def fun(lhs, rhs):
      return lax.conv_general_dilated(
          lhs, rhs, strides, padding, lhs_dilation, rhs_dilation,
          dimension_numbers)

    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  # TODO(mattjj): test conv_general_dilated against numpy

  @staticmethod
  def _conv_transpose_via_grad(data, kernel, strides, padding,
                               dimension_numbers=None):
    """Helper method: calculates conv transpose via grad for testing."""
    assert len(data.shape) == len(kernel.shape)
    nspatial = len(data.shape) - 2
    one = (1,) * nspatial
    dn = lax.conv_dimension_numbers(data.shape, kernel.shape,
                                    dimension_numbers)
    in_shape = onp.take(data.shape, dn.lhs_spec)
    in_sdims = in_shape[2:]
    k_shape = onp.take(kernel.shape, dn.rhs_spec)
    k_sdims = k_shape[2:]
    if padding == 'VALID':
      o_sdims = [in_sdims[i]*strides[i] + max(k_sdims[i]-strides[i],0)
                 for i in range(nspatial)]
    elif padding == 'SAME':
      o_sdims = [in_sdims[i]*strides[i] for i in range(nspatial)]
    o_shape =  [in_shape[0], k_shape[1]] + o_sdims
    out_spec_inv = [x[0] for x in
                    sorted(enumerate(dn.out_spec), key=lambda x: x[1])]
    o_layout = onp.take(onp.array(o_shape), out_spec_inv)
    placeholder = onp.ones(o_layout, data.dtype)
    conv = lambda x: lax.conv_general_dilated(x, kernel, strides, padding,
                                              one, one, dn)
    _, g = api.vjp(conv, placeholder)
    return g(data)[0]

  @staticmethod
  def _transpose_conv_kernel(data, kernel, dimension_numbers):
    dn = lax.conv_dimension_numbers(data.shape, kernel.shape,
                                    dimension_numbers)
    spatial_axes = onp.array(dn.rhs_spec)[2:]
    for axis in spatial_axes:
      kernel = onp.flip(kernel, axis)
    kernel = onp.swapaxes(kernel, dn.rhs_spec[0], dn.rhs_spec[1])
    return kernel

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rng": rng, 'dspec': dspec}
      for lhs_shape, rhs_shape in [
          ((b, 9, 10, i), (k, k, j, i))  # NB: i,j flipped in RHS for transpose
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1), (2, 2), (3, 3)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHWC', 'HWIO', 'NHWC'),]
      for rng in [jtu.rand_small()]))
  def testConvTranspose2DT(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    # NB: this test calculates conv_transpose performing identically to the
    # lhs-grad of conv.
    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                dimension_numbers=dspec,
                                transpose_kernel=True)

    def fun_via_grad(lhs, rhs):
      return self._conv_transpose_via_grad(lhs, rhs, strides, padding,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun, fun_via_grad, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rng": rng, 'dspec': dspec}
      for lhs_shape, rhs_shape in [
          ((b, 9, 10, i), (k, k, i, j))
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1, 1), (1, 2), (2, 1), (2, 2), (3, 3)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHWC', 'HWIO', 'NHWC'),]
      for rng in [jtu.rand_small()]))
  def testConvTranspose2D(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                dimension_numbers=dspec,
                                transpose_kernel=False)

    def fun_via_grad(lhs, rhs):
      rhs_t = self._transpose_conv_kernel(lhs, rhs, dimension_numbers=dspec)
      return self._conv_transpose_via_grad(lhs, rhs_t, strides, padding,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun, fun_via_grad, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype), strides, padding),
          "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
          "strides": strides, "padding": padding, "rng": rng, 'dspec': dspec}
      for lhs_shape, rhs_shape in [
          ((b, 10, i), (k, i, j))
          for b, i, j, k in itertools.product([2,3],[2,3],[2,3],[3,4,5])]
      for dtype in float_dtypes
      for strides in [(1,), (2,), (3,)]
      for padding in ["VALID", "SAME"]
      for dspec in [('NHC', 'HIO', 'NHC'),]
      for rng in [jtu.rand_small()]))
  def testConvTranspose1D(self, lhs_shape, rhs_shape, dtype, strides,
                          padding, dspec, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.conv_transpose(lhs, rhs, strides, padding,
                                dimension_numbers=dspec,
                                transpose_kernel=False)

    def fun_via_grad(lhs, rhs):
      rhs_t = self._transpose_conv_kernel(lhs, rhs, dimension_numbers=dspec)
      return self._conv_transpose_via_grad(lhs, rhs_t, strides, padding,
                                           dimension_numbers=dspec)

    # NB: below just checks for agreement, we're not calling numpy.
    self._CheckAgainstNumpy(fun, fun_via_grad, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_precision={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype),
          precision),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "precision": precision, "rng": rng}
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      for dtype in default_dtypes
      for precision in [None, lax.Precision.DEFAULT, lax.Precision.HIGH,
                        lax.Precision.HIGHEST]
      for rng in [jtu.rand_default()]))
  def testDot(self, lhs_shape, rhs_shape, dtype, precision, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    self._CompileAndCheck(partial(lax.dot, precision=precision), args_maker,
                          check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "rng": rng}
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDotAgainstNumpy(self, lhs_shape, rhs_shape, dtype, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    self._CheckAgainstNumpy(lax.dot, lax_reference.dot, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_lhs_contracting={}_rhs_contracting={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               lhs_contracting, rhs_contracting),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "lhs_contracting": lhs_contracting, "rhs_contracting": rhs_contracting,
       "rng": rng}
      for lhs_shape, rhs_shape, lhs_contracting, rhs_contracting in [
          [(3, 5), (2, 5), [1], [1]],
          [(5, 3), (5, 2), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0,2], [0,1]],
          [(1, 2, 2, 3), (1, 2, 3, 1), [1], [1]],
          [(3, 2), (2, 4), [1], [0]],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_small()]))
  def testDotGeneralContractOnly(self, lhs_shape, rhs_shape, dtype,
                                 lhs_contracting, rhs_contracting, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    dimension_numbers = ((lhs_contracting, rhs_contracting), ([], []))

    def fun(lhs, rhs):
      return lax.dot_general(lhs, rhs, dimension_numbers)

    self._CompileAndCheck(fun, args_maker, check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers, "rng": rng}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
          ((3, 4, 2, 4), (3, 4, 3, 2), (([2], [3]), ([0, 1], [0, 1]))),
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_small()]))
  def testDotGeneralContractAndBatch(self, lhs_shape, rhs_shape, dtype,
                                     dimension_numbers, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    def fun(lhs, rhs):
      return lax.dot_general(lhs, rhs, dimension_numbers)

    self._CompileAndCheck(fun, args_maker, check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers, "rng": rng}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
          ((3, 4, 2, 4), (3, 4, 3, 2), (([2], [3]), ([0, 1], [0, 1]))),
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_small()]))
  def testDotGeneralAgainstNumpy(self, lhs_shape, rhs_shape, dtype,
                                 dimension_numbers, rng):
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    op = lambda x, y: lax.dot_general(x, y, dimension_numbers)
    numpy_op = lambda x, y: lax_reference.dot_general(x, y, dimension_numbers)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_dtype={}_broadcast_sizes={}".format(
          shape, onp.dtype(dtype).name, broadcast_sizes),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes,
       "rng": rng}
      for shape in [(), (2, 3)]
      for dtype in default_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]
      for rng in [jtu.rand_default()]))
  def testBroadcast(self, shape, dtype, broadcast_sizes, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.broadcast(x, broadcast_sizes)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_broadcast_sizes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), broadcast_sizes),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes,
       "rng": rng}
      for shape in [(), (2, 3)]
      for dtype in default_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]
      for rng in [jtu.rand_default()]))
  def testBroadcastAgainstNumpy(self, shape, dtype, broadcast_sizes, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.broadcast(x, broadcast_sizes)
    numpy_op = lambda x: lax_reference.broadcast(x, broadcast_sizes)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions, "rng": rng}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testBroadcastInDim(self, inshape, dtype, outshape, dimensions, rng):
    args_maker = lambda: [rng(inshape, dtype)]
    op = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions, "rng": rng}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testBroadcastInDimAgainstNumpy(self, inshape, dtype, outshape,
                                     dimensions, rng):
    args_maker = lambda: [rng(inshape, dtype)]
    op = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    numpy_op = lambda x: lax_reference.broadcast_in_dim(x, outshape, dimensions)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype)),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype,
       "rng": rng}
      for dtype in default_dtypes
      for arg_shape, out_shape in [
          [(3, 4), (12,)], [(2, 1, 4), (8,)], [(2, 2, 4), (2, 8)]
      ]
      for rng in [jtu.rand_default()]))
  def testReshape(self, arg_shape, out_shape, dtype, rng):
    args_maker = lambda: [rng(arg_shape, dtype)]
    op = lambda x: lax.reshape(x, out_shape)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype)),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype,
       "rng": rng}
      for dtype in default_dtypes
      for arg_shape, out_shape in [
          [(3, 4), (12,)], [(2, 1, 4), (8,)], [(2, 2, 4), (2, 8)]
      ]
      for rng in [jtu.rand_default()]))
  def testReshapeAgainstNumpy(self, arg_shape, out_shape, dtype, rng):
    args_maker = lambda: [rng(arg_shape, dtype)]
    op = lambda x: lax.reshape(x, out_shape)
    numpy_op = lambda x: lax_reference.reshape(x, out_shape)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads),
       "shape": shape, "dtype": dtype, "pads": pads, "rng": jtu.rand_small()}
      for shape in [(2, 3)]
      for dtype in default_dtypes
      for pads in [[(1, 2, 1), (0, 1, 0)]]))
  def testPad(self, shape, dtype, pads, rng):
    args_maker = lambda: [rng(shape, dtype)]
    fun = lambda operand: lax.pad(operand, onp.array(0, dtype), pads)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads),
       "shape": shape, "dtype": dtype, "pads": pads, "rng": jtu.rand_small()}
      for shape in [(2, 3)]
      for dtype in default_dtypes
      for pads in [[(1, 2, 1), (0, 1, 0)]]))
  def testPadAgainstNumpy(self, shape, dtype, pads, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.pad(x, onp.array(0, dtype), pads)
    numpy_op = lambda x: lax_reference.pad(x, onp.array(0, dtype), pads)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  def testReverse(self):
    rev = api.jit(lambda operand: lax.rev(operand, dimensions))

    dimensions = [0]
    self.assertAllClose(onp.array([3, 2, 1]), rev(onp.array([1, 2, 3])),
                        check_dtypes=False)

    dimensions = [0, 1]
    self.assertAllClose(onp.array([[6, 5, 4], [3, 2, 1]]),
                        rev(onp.array([[1, 2, 3], [4, 5, 6]])),
                        check_dtypes=False)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}".format(
          jtu.format_shape_dtype_string(pred_shape, onp.bool_),
          jtu.format_shape_dtype_string(arg_shape, arg_dtype)),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "arg_dtype": arg_dtype,
       "rng": rng}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for arg_dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSelect(self, pred_shape, arg_shape, arg_dtype, rng):

    def args_maker():
      return [rng(pred_shape, onp.bool_), rng(arg_shape, arg_dtype),
              rng(arg_shape, arg_dtype)]

    return self._CompileAndCheck(lax.select, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}".format(
          jtu.format_shape_dtype_string(pred_shape, onp.bool_),
          jtu.format_shape_dtype_string(arg_shape, arg_dtype)),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "arg_dtype": arg_dtype,
       "rng": rng}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for arg_dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSelectAgainstNumpy(self, pred_shape, arg_shape, arg_dtype, rng):

    def args_maker():
      return [rng(pred_shape, onp.bool_), rng(arg_shape, arg_dtype),
              rng(arg_shape, arg_dtype)]

    return self._CheckAgainstNumpy(lax.select, lax_reference.select, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides, "rng": rng}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSlice(self, shape, dtype, starts, limits, strides, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.slice(x, starts, limits, strides)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides, "rng": rng}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSliceAgainstNumpy(self, shape, dtype, starts, limits,
                            strides, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.slice(x, starts, limits, strides)
    numpy_op = lambda x: lax_reference.slice(x, starts, limits, strides)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_size_indices={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, size_indices),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "size_indices": size_indices, "rng": rng}
      for shape, start_indices, size_indices in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicSlice(self, shape, dtype, start_indices, size_indices, rng):
    args_maker = lambda: [rng(shape, dtype), onp.array(start_indices)]
    op = lambda x, starts: lax.dynamic_slice(x, starts, size_indices)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_size_indices={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, size_indices),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "size_indices": size_indices, "rng": rng}
      for shape, start_indices, size_indices in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicSliceAgainstNumpy(self, shape, dtype, start_indices,
                                   size_indices, rng):
    args_maker = lambda: [rng(shape, dtype), onp.array(start_indices)]
    op = lambda x, s: lax.dynamic_slice(x, s, size_indices)
    numpy_op = lambda x, s: lax_reference.dynamic_slice(x, s, size_indices)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_update_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, update_shape),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "update_shape": update_shape, "rng": rng}
      for shape, start_indices, update_shape in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicUpdateSlice(self, shape, dtype, start_indices, update_shape,
                             rng):

    def args_maker():
      return [rng(shape, dtype), rng(update_shape, dtype),
              onp.array(start_indices)]

    self._CompileAndCheck(lax.dynamic_update_slice, args_maker,
                          check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_update_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, update_shape),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "update_shape": update_shape, "rng": rng}
      for shape, start_indices, update_shape in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicUpdateSliceAgainstNumpy(self, shape, dtype, start_indices,
                                         update_shape, rng):

    def args_maker():
      return [rng(shape, dtype), rng(update_shape, dtype),
              onp.array(start_indices)]

    self._CheckAgainstNumpy(lax.dynamic_update_slice,
                            lax_reference.dynamic_update_slice, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm),
       "shape": shape, "dtype": dtype, "perm": perm, "rng": rng}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testTranspose(self, shape, dtype, perm, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.transpose(x, perm)
    self._CompileAndCheck(op, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm),
       "shape": shape, "dtype": dtype, "perm": perm, "rng": rng}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testTransposeAgainstNumpy(self, shape, dtype, perm, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.transpose(x, perm)
    numpy_op = lambda x: lax_reference.transpose(x, perm)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_inshape={}_reducedims={}_initval={}"
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype), dims,
               init_val),
       "op": op, "init_val": init_val, "shape": shape, "dtype": dtype,
       "dims": dims, "rng": rng}
      for init_val, op, dtypes in [
          (0, lax.add, default_dtypes),
          (1, lax.mul, default_dtypes),
          (0, lax.max, all_dtypes), # non-monoidal
          (-onp.inf, lax.max, float_dtypes),
          (onp.iinfo(onp.int32).min, lax.max, [onp.int32]),
          # (onp.iinfo(onp.int64).min, lax.max, [onp.int64]),  # TODO fails
          (onp.iinfo(onp.uint32).min, lax.max, [onp.uint32]),
          (onp.iinfo(onp.uint64).min, lax.max, [onp.uint64]),
          (onp.inf, lax.min, float_dtypes),
          (onp.iinfo(onp.int32).max, lax.min, [onp.int32]),
          # (onp.iinfo(onp.int64).max, lax.min, [onp.int64]),  # TODO fails
          (onp.iinfo(onp.uint32).max, lax.min, [onp.uint32]),
          (onp.iinfo(onp.uint64).max, lax.min, [onp.uint64]),
      ]
      for dtype in dtypes
      for shape, dims in [
          [(3, 4, 5), (0,)], [(3, 4, 5), (1, 2)],
          [(3, 4, 5), (0, 2)], [(3, 4, 5), (0, 1, 2)]
      ]
      for rng in [jtu.rand_default() if onp.issubdtype(dtype, onp.integer)
                  else jtu.rand_small()]))
  def testReduce(self, op, init_val, shape, dtype, dims, rng):
    init_val = onp.asarray(init_val, dtype=dtype)
    fun = lambda operand, init_val: lax.reduce(operand, init_val, op, dims)
    args_maker = lambda: [rng(shape, dtype), init_val]
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

    # we separately test the version that uses a concrete init_val because it
    # can hit different code paths
    fun = lambda operand: lax.reduce(operand, init_val, op, dims)
    args_maker = lambda: [rng(shape, dtype)]
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_dtype={}_padding={}"
       .format(op.__name__, onp.dtype(dtype).name, padding),
       "op": op, "init_val": init_val, "dtype": dtype, "padding": padding,
       "rng": rng}
      for init_val, op, dtypes in [
          (0, lax.add, [onp.float32]),
          (-onp.inf, lax.max, [onp.float32]),
          (onp.inf, lax.min, [onp.float32]),
      ]
      for dtype in dtypes
      for padding in ["VALID", "SAME"]
      for rng in [jtu.rand_small()]))
  def testReduceWindow(self, op, init_val, dtype, padding, rng):
    init_val = onp.asarray(init_val, dtype=dtype)

    all_configs = itertools.chain(
        itertools.product(
            [(4, 6)],
            [(2, 1), (1, 2)],
            [(1, 1), (2, 1), (1, 2)]),
        itertools.product(
            [(3, 2, 4, 6)], [(1, 1, 2, 1), (2, 1, 2, 1)],
            [(1, 2, 2, 1), (1, 1, 1, 1)]))

    def fun(operand, init_val):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding)

    # pylint: disable=cell-var-from-loop
    for shape, dims, strides in all_configs:
      args_maker = lambda: [rng(shape, dtype), init_val]
      self._CompileAndCheck(fun, args_maker, check_dtypes=True)
    # pylint: enable=cell-var-from-loop

    # we separately test the version that uses a concrete init_val because it
    # can hit different code paths
    def fun(operand):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding)

    # pylint: disable=cell-var-from-loop
    for shape, dims, strides in all_configs:
      args_maker = lambda: [rng(shape, dtype)]
      self._CompileAndCheck(fun, args_maker, check_dtypes=True)
    # pylint: enable=cell-var-from-loop

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axis),
       "rng": rng, "shape": shape, "dtype": dtype, "axis": axis}
      for dtype in [onp.float32, onp.int32, onp.uint32]
      for shape in [(5,), (5, 7)]
      for axis in [-1, len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSort(self, shape, dtype, axis, rng):
    args_maker = lambda: [rng(shape, dtype)]
    fun = lambda x: lax.sort(x, axis)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axis),
       "rng": rng, "shape": shape, "dtype": dtype, "axis": axis}
      for dtype in [onp.float32, onp.int32, onp.uint32]
      for shape in [(5,), (5, 7)]
      for axis in [-1, len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSortAgainstNumpy(self, shape, dtype, axis, rng):
    args_maker = lambda: [rng(shape, dtype)]
    op = lambda x: lax.sort(x, axis)
    numpy_op = lambda x: lax_reference.sort(x, axis)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_keyshape={}_valshape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, key_dtype),
          jtu.format_shape_dtype_string(shape, val_dtype),
          axis),
       "rng": rng, "shape": shape,
       "key_dtype": key_dtype, "val_dtype": val_dtype, "axis": axis}
      for key_dtype in [onp.float32, onp.int32, onp.uint32]
      for val_dtype in [onp.float32, onp.int32, onp.uint32]
      for shape in [(3,), (5, 3)]
      for axis in [-1, len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSortKeyVal(self, shape, key_dtype, val_dtype, axis, rng):
    # This test relies on the property that wherever keys are tied, values are
    # too, since we don't guarantee the same ordering of values with equal keys.
    # To avoid that case, we generate unique keys (globally in the key array).
    perm_rng = onp.random.RandomState(0)
    def args_maker():
      flat_keys = onp.arange(onp.prod(shape, dtype=int), dtype=key_dtype)
      keys = perm_rng.permutation(flat_keys).reshape(shape)
      values = rng(shape, val_dtype)
      return keys, values

    fun = lambda keys, values: lax.sort_key_val(keys, values, axis)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_keyshape={}_valshape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, key_dtype),
          jtu.format_shape_dtype_string(shape, val_dtype),
          axis),
       "rng": rng, "shape": shape,
       "key_dtype": key_dtype, "val_dtype": val_dtype, "axis": axis}
      for key_dtype in [onp.float32, onp.int32, onp.uint32]
      for val_dtype in [onp.float32, onp.int32, onp.uint32]
      for shape in [(3,), (5, 3)]
      for axis in [-1, len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSortKeyValAgainstNumpy(self, shape, key_dtype, val_dtype, axis, rng):
    # This test relies on the property that wherever keys are tied, values are
    # too, since we don't guarantee the same ordering of values with equal keys.
    # To avoid that case, we generate unique keys (globally in the key array).
    perm_rng = onp.random.RandomState(0)
    def args_maker():
      flat_keys = onp.arange(onp.prod(shape, dtype=int), dtype=key_dtype)
      keys = perm_rng.permutation(flat_keys).reshape(shape)
      values = rng(shape, val_dtype)
      return keys, values

    op = lambda ks, vs: lax.sort_key_val(ks, vs, axis)
    numpy_op = lambda ks, vs: lax_reference.sort_key_val(ks, vs, axis)
    self._CheckAgainstNumpy(op, numpy_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "rng": rng}
      for lhs_shape, rhs_shape in [((3, 2), (2, 4)),
                                   ((5, 3, 2), (5, 2, 4)),
                                   ((1, 2, 2, 3), (1, 2, 3, 1))]
      for dtype in float_dtypes
      for rng in [jtu.rand_small()]))
  def testBatchMatMul(self, lhs_shape, rhs_shape, dtype, rng):
    arg_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]
    self._CompileAndCheck(lax.batch_matmul, arg_maker, check_dtypes=True)

  def testCollapse(self):

    @api.jit
    def collapse_first_two(x):
      return lax.collapse(x, 0, 2)

    self.assertEqual((6,), collapse_first_two(onp.zeros((2, 3))).shape)
    self.assertEqual((6, 4), collapse_first_two(onp.zeros((2, 3, 4))).shape)
    self.assertEqual((2, 3, 4),
                     collapse_first_two(onp.zeros((1, 2, 3, 4))).shape)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_axes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, axes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "axes": axes, "rng": rng}
      for dtype in all_dtypes
      for shape, idxs, axes in [
          [(3, 4, 5), (onp.array([0, 2, 1]),), (0,)],
          [(3, 4, 5), (onp.array([-1, -2]),), (0,)],
          [(3, 4, 5), (onp.array([0, 2]), onp.array([1, 3])), (0, 1)],
          [(3, 4, 5), (onp.array([0, 2]), onp.array([1, 3])), (0, 2)],
      ]
      for rng in [jtu.rand_default()]))
  def testIndexTake(self, shape, dtype, idxs, axes, rng):
    rand_idxs = lambda: tuple(rng(e.shape, e.dtype) for e in idxs)
    args_maker = lambda: [rng(shape, dtype), rand_idxs()]
    fun = lambda src, idxs: lax.index_take(src, idxs, axes)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_dnums={}_slice_sizes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, dnums,
          slice_sizes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "dnums": dnums,
       "slice_sizes": slice_sizes, "rng": rng, "rng_idx": rng_idx}
      for dtype in all_dtypes
      for shape, idxs, dnums, slice_sizes in [
          ((5,), onp.array([[0], [2]]), lax.GatherDimensionNumbers(
            offset_dims=(), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1,)),
          ((10,), onp.array([[0], [0], [0]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(), start_index_map=(0,)),
            (2,)),
          ((10, 5,), onp.array([[0], [2], [1]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1, 3)),
          ((10, 5), onp.array([[0, 2], [1, 0]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(0,), start_index_map=(0, 1)),
            (1, 3)),
      ]
      for rng_idx in [jtu.rand_int(max(shape))]
      for rng in [jtu.rand_default()]))
  def testGather(self, shape, dtype, idxs, dnums, slice_sizes, rng, rng_idx):
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(shape, dtype), rand_idxs()]
    fun = partial(lax.gather, dimension_numbers=dnums, slice_sizes=slice_sizes)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatterAdd(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                     rng_idx):
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_add, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatterMin(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                     rng_idx):
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_min, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatterMax(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                     rng_idx):
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter_max, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatter(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                  rng_idx):
    rand_idxs = lambda: rng_idx(idxs.shape, idxs.dtype)
    args_maker = lambda: [rng(arg_shape, dtype), rand_idxs(),
                          rng(update_shape, dtype)]
    fun = partial(lax.scatter, dimension_numbers=dnums)
    self._CompileAndCheck(fun, args_maker, check_dtypes=True)

  def testLongConstantHandling(self):
    if six.PY3:
      self.skipTest("Test is Python 2 specific")
    self.assertTrue(api.jit(lambda x: lax.lt(x, long(10)))(long(3)))  # noqa: F821

  def testIssue831(self):
    # Tests the DeviceTuple constant handler
    def f(x):
      g = lambda *args: args[1]
      return api.jit(lax.fori_loop, static_argnums=(2,))( 0, 10, g, x)

    api.jit(f)(1.)  # doesn't crash

  def testReshapeWithUnusualShapes(self):
    ans = lax.reshape(onp.ones((3,), onp.float32), (lax.add(1, 2), 1))
    self.assertAllClose(ans, onp.ones((3, 1), onp.float32), check_dtypes=True)

    jtu.check_raises_regexp(
      lambda: lax.reshape(onp.ones(3,), (onp.array([3, 1]),)), TypeError,
      "Shapes must be 1D sequences of concrete values of integer type.*")

    jtu.check_raises_regexp(
      lambda: lax.reshape(onp.ones(3,), (1.5, 2.0)), TypeError,
      "Shapes must be 1D sequences of concrete values of integer type.*")


class DeviceConstantTest(jtu.JaxTestCase):
  def _CheckDeviceConstant(self, make_const, expected):
    # check casting to ndarray works
    asarray_result = onp.asarray(make_const())

    # check passing as an argument works (should hit constant handler)
    zero = onp.array(0, expected.dtype)
    argument_result = lax.add(zero, make_const())

    # check looping into a compiled computation works
    jit_result = api.jit(lambda x: lax.add(x, make_const()))(zero)

    # ensure they're all the same
    self.assertAllClose(asarray_result, expected, check_dtypes=True)
    self.assertAllClose(argument_result, expected, check_dtypes=True)
    self.assertAllClose(jit_result, expected, check_dtypes=True)

    # ensure repr doesn't crash
    repr(make_const())

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_fill={}".format(
          jtu.format_shape_dtype_string(shape, dtype) if dtype else shape,
          fill_value),
       "shape": shape, "dtype": dtype, "fill_value": fill_value}
      for dtype in itertools.chain(default_dtypes, [None])
      for shape in [(), (3,), (2, 3), (2, 3, 4), (1001, 1001)]
      for fill_value in [0, 1, onp.pi]))
  def testFilledConstant(self, shape, fill_value, dtype):
    make_const = lambda: lax.full(shape, fill_value, dtype)
    expected = onp.full(shape, fill_value, dtype)
    self._CheckDeviceConstant(make_const, expected)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_dim={}".format(
          jtu.format_shape_dtype_string(shape, dtype), dimension),
       "shape": shape, "dtype": dtype, "dimension": dimension}
      for dtype in default_dtypes
      for shape in [(), (3,), (2, 3), (2, 3, 4),
                    # TODO(mattjj): re-enable
                    # (1001, 1001), (101, 101, 101),
                    ]
      for dimension in range(len(shape))))
  def testIotaConstant(self, dtype, shape, dimension):
    make_const = lambda: lax.broadcasted_iota(dtype, shape, dimension)

    arr = onp.arange(shape[dimension], dtype=xla_bridge.canonicalize_dtype(dtype))
    singleton_shape = [1] * len(shape)
    singleton_shape[dimension] = shape[dimension]
    expected = onp.broadcast_to(arr.reshape(singleton_shape), shape)

    self._CheckDeviceConstant(make_const, expected)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_{}_axes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axes),
       "shape": shape, "dtype": dtype, "axes": axes}
      for dtype in default_dtypes
      for shape, axes in [
          [(2, 3), (0, 1)],
          [(2, 3, 4), (0, 1)],
          [(2, 3, 4), (0, 2)],
          [(2, 3, 4), (1, 2)],
          [(2, 3, 4), (0, 1, 2)],
          [(2, 3, 4, 2), (0, 1, 2)],
          [(2, 3, 4, 2), (0, 2, 3)],
          [(1001, 1001), (0, 1)],
      ]))
  def testEyeConstant(self, dtype, shape, axes):
    make_const = lambda: lax.broadcasted_eye(dtype, shape, axes)

    # don't check the asarray case, just assume it's right
    expected = onp.asarray(make_const())

    self._CheckDeviceConstant(make_const, expected)


GradTestSpec = collections.namedtuple(
    "GradTestSpec", ["op", "nargs", "order", "rng", "dtypes", "name", "tol"])
def grad_test_spec(op, nargs, order, rng, dtypes, name=None, tol=None):
  return GradTestSpec(op, nargs, order, rng, dtypes, name or op.__name__, tol)

LAX_GRAD_OPS = [
    grad_test_spec(lax.neg, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.floor, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),
    grad_test_spec(lax.ceil, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),
    grad_test_spec(lax.round, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),

    grad_test_spec(lax.exp, nargs=1, order=2, rng=jtu.rand_small(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.expm1, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.log, nargs=1, order=2, rng=jtu.rand_positive(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.log1p, nargs=1, order=2, rng=jtu.rand_positive(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.sinh, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64], tol=1e-5),
    grad_test_spec(lax.cosh, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64], tol=1e-5),
    grad_test_spec(lax.tanh, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64], tol=1e-5),
    grad_test_spec(lax.sin, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.cos, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.tan, nargs=1, order=2, rng=jtu.rand_uniform(-1.3, 1.3),
                   dtypes=[onp.float64, onp.complex64], tol=1e-3),
    grad_test_spec(lax.asin, nargs=1, order=2, rng=jtu.rand_uniform(-1., 1.),
                   dtypes=[onp.float64], tol=1e-3),
    grad_test_spec(lax.acos, nargs=1, order=2, rng=jtu.rand_uniform(-1., 1.),
                   dtypes=[onp.float64], tol=1e-3),
    # TODO(proteneer): atan2 input is already a representation of a
    # complex number. Need to think harder about what this even means
    # if each input itself is a complex number.
    grad_test_spec(lax.atan2, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),

    grad_test_spec(lax.erf, nargs=1, order=2, rng=jtu.rand_small(),
                   dtypes=[onp.float64]),
    grad_test_spec(lax.erfc, nargs=1, order=2, rng=jtu.rand_small(),
                   dtypes=[onp.float64]),
    grad_test_spec(lax.erf_inv, nargs=1, order=2, rng=jtu.rand_small(),
                   dtypes=[onp.float64]),
    # grad_test_spec(lax.lgamma, nargs=1, order=2, rng=jtu.rand_small(),
    #                dtypes=[onp.float64]),  # TODO(mattjj): enable

    grad_test_spec(lax.real, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.complex64]),
    grad_test_spec(lax.imag, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.complex64]),
    grad_test_spec(lax.complex, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float32]),
    grad_test_spec(lax.conj, nargs=1, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float32, onp.complex64]),
    grad_test_spec(lax.abs, nargs=1, order=2, rng=jtu.rand_positive(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.pow, nargs=2, order=2, rng=jtu.rand_positive(),
                   dtypes=[onp.float64, onp.complex64]),

    grad_test_spec(lax.add, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.sub, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.mul, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64, onp.complex64]),
    grad_test_spec(lax.div, nargs=2, order=1, rng=jtu.rand_not_small(),
                   dtypes=[onp.float64, onp.complex64]),

    grad_test_spec(lax.max, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),
    grad_test_spec(lax.min, nargs=2, order=2, rng=jtu.rand_default(),
                   dtypes=[onp.float64]),
    # TODO(mattjj): make some-equal checks more robust, enable second-order
    # grad_test_spec(lax.max, nargs=2, order=1, rng=jtu.rand_some_equal(),
    #                dtypes=[onp.float64], name="MaxSomeEqual"),
    # grad_test_spec(lax.min, nargs=2, order=1, rng=jtu.rand_some_equal(),
    #                dtypes=[onp.float64], name="MinSomeEqual"),
]

GradSpecialValuesTestSpec = collections.namedtuple(
    "GradSpecialValuesTestSpec", ["op", "values"])

LAX_GRAD_SPECIAL_VALUE_TESTS = [
    GradSpecialValuesTestSpec(lax.sinh, [0.]),
    GradSpecialValuesTestSpec(lax.cosh, [0.]),
    GradSpecialValuesTestSpec(lax.tanh, [0., 1000.]),
    GradSpecialValuesTestSpec(lax.sin, [0., onp.pi, onp.pi/2., onp.pi/4.]),
    GradSpecialValuesTestSpec(lax.cos, [0., onp.pi, onp.pi/2., onp.pi/4.]),
    GradSpecialValuesTestSpec(lax.tan, [0.]),
    GradSpecialValuesTestSpec(lax.asin, [0.]),
    GradSpecialValuesTestSpec(lax.acos, [0.]),
    GradSpecialValuesTestSpec(lax.atan, [0., 1000.]),
    GradSpecialValuesTestSpec(lax.erf, [0., 10.]),
    GradSpecialValuesTestSpec(lax.erfc, [0., 10.]),
]


def check_grads_bilinear(f, args, order,
                         modes=["fwd", "rev"], atol=None, rtol=None):
  # Can use large eps to make up for numerical inaccuracies since the op is
  # bilinear (relying on the fact that we only check one arg at a time)
  lhs, rhs = args
  check_grads(lambda lhs: f(lhs, rhs), (lhs,), order,
              modes=modes, atol=atol, rtol=rtol, eps=1.)
  check_grads(lambda rhs: f(lhs, rhs), (rhs,), order,
              modes=modes, atol=atol, rtol=rtol, eps=1.)


class LaxAutodiffTest(jtu.JaxTestCase):

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": jtu.format_test_name_suffix(
            rec.name, shapes, itertools.repeat(dtype)),
         "op": rec.op, "rng": rec.rng, "shapes": shapes, "dtype": dtype,
         "order": rec.order, "tol": rec.tol}
        for shape_group in compatible_shapes
        for shapes in CombosWithReplacement(shape_group, rec.nargs)
        for dtype in rec.dtypes)
      for rec in LAX_GRAD_OPS))
  def testOpGrad(self, op, rng, shapes, dtype, order, tol):
    if jtu.device_under_test() == "tpu" and op is lax.pow:
      raise SkipTest("pow grad imprecise on tpu")
    tol = 1e-1 if num_float_bits(dtype) == 32 else tol
    args = tuple(rng(shape, dtype) for shape in shapes)
    check_grads(op, args, order, ["fwd", "rev"], tol, tol)

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
          {"testcase_name": "_{}_{}".format(rec.op.__name__, special_value),
           "op": rec.op, "special_value": special_value}
          for special_value in rec.values)
      for rec in LAX_GRAD_SPECIAL_VALUE_TESTS))
  def testOpGradSpecialValue(self, op, special_value):
    check_grads(op, (special_value,), 2, ["fwd", "rev"])

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_from_dtype={}_to_dtype={}".format(
          jtu.dtype_str(from_dtype), jtu.dtype_str(to_dtype)),
       "from_dtype": from_dtype, "to_dtype": to_dtype, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          float_dtypes + complex_dtypes, repeat=2)
      for rng in [jtu.rand_default()]))
  def testConvertElementTypeGrad(self, from_dtype, to_dtype, rng):
    args = (rng((2, 3), from_dtype),)
    convert_element_type = lambda x: lax.convert_element_type(x, to_dtype)
    check_grads(convert_element_type, args, 2, ["fwd", "rev"], 1e-3, 1e-3, 1e-3)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}".format(
          jtu.format_shape_dtype_string(min_shape, dtype),
          jtu.format_shape_dtype_string(operand_shape, dtype),
          jtu.format_shape_dtype_string(max_shape, dtype)),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype, "rng": rng}
      for min_shape, operand_shape, max_shape in [
          [(), (), ()],
          [(), (2, 3), ()],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testClampGrad(self, min_shape, operand_shape, max_shape, dtype, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    shapes = [min_shape, operand_shape, max_shape]
    min, operand, max = (rng(shape, dtype) for shape in shapes)
    min, max = onp.minimum(min, max), onp.maximum(min, max)  # broadcast
    check_grads(lax.clamp, (min, operand, max), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_dim={}_baseshape=[{}]_dtype={}_narrs={}".format(
          dim, ",".join(str(d) for d in base_shape), onp.dtype(dtype).name,
          num_arrs),
       "dim": dim, "base_shape": base_shape, "dtype": dtype,
       "num_arrs": num_arrs, "rng": rng}
      for num_arrs in [3]
      for dtype in float_dtypes
      for base_shape in [(4,), (3, 4), (2, 3, 4)]
      for dim in range(len(base_shape))
      for rng in [jtu.rand_default()]))
  def testConcatenateGrad(self, dim, base_shape, dtype, num_arrs, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    shapes = [base_shape[:dim] + (size,) + base_shape[dim+1:]
              for size, _ in zip(itertools.cycle([3, 1, 4]), range(num_arrs))]
    operands = tuple(rng(shape, dtype) for shape in shapes)
    concatenate = lambda *args: lax.concatenate(args, dim)
    check_grads(concatenate, operands, 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               strides, padding),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "rng": rng,}
       for lhs_shape, rhs_shape, all_strides in itertools.chain(
           [((b, i, 3, 4), (j, i, 1, 2), [(1, 1), (1, 2), (2, 1)])
            for b, i, j in itertools.product([2, 3], repeat=3)],
           [((4, 2, 1), (3, 2, 1), [(1,)])])
       for strides in all_strides
       for dtype in float_dtypes
       for padding in ["VALID", "SAME"]
       for rng in [jtu.rand_small()]))
  def testConvGrad(self, lhs_shape, rhs_shape, dtype, strides, padding, rng):
    lhs = rng(lhs_shape, dtype)
    rhs = rng(rhs_shape, dtype)
    conv = partial(lax.conv, window_strides=strides, padding=padding,
                   precision=lax.Precision.HIGHEST)
    check_grads_bilinear(conv, (lhs, rhs), order=2, modes=["fwd", "rev"],
                         atol=1e-2, rtol=1e-2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_lhs_dilation={}_"
       "rhs_dilation={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               strides, padding, lhs_dil, rhs_dil),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dil": lhs_dil,
       "rhs_dil": rhs_dil, "rng": rng}
       for lhs_shape, rhs_shape, all_strides, all_pads, lhs_dils, rhs_dils in
       itertools.chain(
           [((b, i, 3, 4), (j, i, 1, 2), [(1, 1), (1, 2), (2, 1)],
             [((0, 0), (0, 0)), ((-1, 0), (0, -1)), ((1, 0), (0, 1))],
             [(1, 1), (2, 1)], [(1, 1)])
            for b, i, j in itertools.product([2, 3], repeat=3)],
           [((4, 2, 1), (3, 2, 1), [(1,)], [((1, 1),), ((0, 0),)],
             [(1,), (2,)], [(1,), (2,)])])
       for strides in all_strides
       for rhs_dil in rhs_dils
       for lhs_dil in lhs_dils
       for dtype in float_dtypes
       for padding in all_pads
       for rng in [jtu.rand_small()]))
  def testConvWithGeneralPaddingGrad(self, lhs_shape, rhs_shape, dtype, strides,
                                     padding, lhs_dil, rhs_dil, rng):
    lhs = rng(lhs_shape, dtype)
    rhs = rng(rhs_shape, dtype)
    conv = partial(lax.conv_with_general_padding, window_strides=strides,
                   padding=padding, lhs_dilation=lhs_dil, rhs_dilation=rhs_dil,
                   precision=lax.Precision.HIGHEST)
    check_grads_bilinear(conv, (lhs, rhs), order=2, modes=["fwd", "rev"],
                         atol=1e-2, rtol=1e-2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_lhs_dilation={}_"
       "rhs_dilation={}_dims={}_feature_group_count={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               strides, padding, lhs_dil, rhs_dil, ",".join(dim_nums),
               feature_group_count),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dil": lhs_dil,
       "rhs_dil": rhs_dil, "rng": rng, "dimension_numbers": dim_nums,
       "perms": perms, "feature_group_count": feature_group_count}
      for lhs_shape, rhs_shape, all_strides, all_pads, lhs_dils, rhs_dils in [
          ((b, i, 6, 7),  # lhs_shape
           (j, i, 1, 2),  # rhs_shape
           [(1, 1), (1, 2), (2, 1)],  # strides
           [((0, 0), (0, 0)), ((1, 0), (0, 1)), ((0, -1), (0, 0))],  # pads
           [(1, 1), (2, 1)],  # lhs_dils
           [(1, 1), (2, 2)])  # rhs_dils
          for b, i, j in itertools.product([1, 2], repeat=3)]
      for feature_group_count in [1, 2]
      for strides in all_strides
      for rhs_dil in rhs_dils
      for lhs_dil in lhs_dils
      for dtype in float_dtypes
      for padding in all_pads
      for dim_nums, perms in [
          (("NCHW", "OIHW", "NCHW"), ([0, 1, 2, 3], [0, 1, 2, 3])),
          (("NHWC", "HWIO", "NHWC"), ([0, 2, 3, 1], [2, 3, 1, 0])),
          (("NHWC", "OIHW", "NCHW"), ([0, 2, 3, 1], [0, 1, 2, 3]))]
      for rng in [jtu.rand_default()]
  ))
  @jtu.skip_on_devices("tpu")  # TODO(phawkins): precision problems on TPU.
  def testConvGeneralDilatedGrad(self, lhs_shape, rhs_shape, dtype, strides,
                                 padding, lhs_dil, rhs_dil, dimension_numbers,
                                 perms, feature_group_count, rng):
    tol = 1e-1 if onp.finfo(dtype).bits == 32 else 1e-3

    # permute shapes to match dim_spec, scale by feature_group_count
    lhs_perm, rhs_perm = perms
    lhs_shape = list(onp.take(lhs_shape, lhs_perm))
    rhs_shape = list(onp.take(rhs_shape, rhs_perm))
    dim_spec = lax.conv_dimension_numbers(lhs_shape, rhs_shape, dimension_numbers)
    lhs_shape[dim_spec.lhs_spec[1]] *= feature_group_count
    rhs_shape[dim_spec.rhs_spec[0]] *= feature_group_count

    lhs = rng(lhs_shape, dtype)
    rhs = rng(rhs_shape, dtype)
    conv = partial(lax.conv_general_dilated, window_strides=strides,
                   padding=padding, lhs_dilation=lhs_dil, rhs_dilation=rhs_dil,
                   dimension_numbers=dimension_numbers,
                   feature_group_count=feature_group_count,
                   precision=lax.Precision.HIGHEST)
    check_grads_bilinear(conv, (lhs, rhs), order=2, modes=["fwd", "rev"],
                         atol=tol, rtol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "rng": jtu.rand_default()}
      for lhs_shape in [(2,), (3, 2)] for rhs_shape in [(2,), (2, 4)]
      for dtype in float_dtypes))
  def testDotGrad(self, lhs_shape, rhs_shape, dtype, rng):
    tol = 1e-1 if num_float_bits(dtype) == 32 else 1e-3
    lhs = rng(lhs_shape, dtype)
    rhs = rng(rhs_shape, dtype)
    dot = partial(lax.dot, precision=lax.Precision.HIGHEST)
    check_grads_bilinear(dot, (lhs, rhs), order=2, modes=["fwd", "rev"],
                         atol=tol, rtol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers, "rng": jtu.rand_small()}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 2), (2, 4), (([1], [0]), ([], []))),
          ((3, 5), (2, 5), (([1], [1]), ([], []))),
          ((5, 3), (5, 2), (([0], [0]), ([], []))),
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
      ]
      for dtype in float_dtypes))
  def testDotGeneralContractAndBatchGrads(self, lhs_shape, rhs_shape, dtype,
                                          dimension_numbers, rng):
    tol = 1e-1 if onp.finfo(dtype).bits == 32 else 1e-2
    lhs = rng(lhs_shape, dtype)
    rhs = rng(rhs_shape, dtype)
    dot_general = partial(lax.dot_general, dimension_numbers=dimension_numbers,
                          precision=lax.Precision.HIGHEST)
    check_grads_bilinear(dot_general, (lhs, rhs), order=2, modes=["fwd", "rev"],
                         atol=tol, rtol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_dtype={}_broadcast_sizes={}".format(
          shape, onp.dtype(dtype).name, broadcast_sizes),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes,
       "rng": rng}
      for shape in [(), (2, 3)]
      for dtype in float_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]
      for rng in [jtu.rand_default()]))
  def testBroadcastGrad(self, shape, dtype, broadcast_sizes, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    args = (rng(shape, dtype),)
    broadcast = lambda x: lax.broadcast(x, broadcast_sizes)
    check_grads(broadcast, args, 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions, "rng": rng}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testBroadcastInDimGrad(self, inshape, dtype, outshape, dimensions, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(inshape, dtype)
    broadcast_in_dim = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    check_grads(broadcast_in_dim, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_perm={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype),
          permutation),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype,
       "rng": rng, "permutation": permutation}
      for dtype in float_dtypes
      for arg_shape, out_shape, permutation in [
          [(3, 4), (12,), None],
          [(2, 1, 4), (8,), None],
          [(2, 2, 4), (2, 8), None],
          [(3, 4), (12,), (0, 1)],
          [(3, 4), (12,), (1, 0)],
          [(2, 1, 4), (8,), (0, 2, 1)],
          [(2, 1, 4), (8,), (2, 0, 1)],
          [(2, 2, 4), (2, 8), (0, 2, 1)],
          [(2, 2, 4), (2, 8), (2, 0, 1)],
      ]
      for rng in [jtu.rand_default()]))
  def testReshapeGrad(self, arg_shape, out_shape, permutation, dtype, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(arg_shape, dtype)
    reshape = lambda x: lax.reshape(x, out_shape, permutation)
    check_grads(reshape, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads),
       "shape": shape, "dtype": dtype, "pads": pads, "rng": jtu.rand_small()}
      for shape in [(2, 3)]
      for dtype in float_dtypes
      for pads in [[(1, 2, 1), (0, 1, 0)], [(-1, 0, 0), (-1, 0, 2)]]))
  def testPadGrad(self, shape, dtype, pads, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None

    operand = rng(shape, dtype)
    pad = lambda operand: lax.pad(operand, onp.array(0, dtype), pads)
    check_grads(pad, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

    operand = rng(shape, dtype)
    padding_value = onp.array(0., dtype)
    pad = lambda operand, padding_value: lax.pad(operand, padding_value, pads)
    check_grads(pad, (operand, padding_value), 2, ["fwd", "rev"], tol, tol, tol)

  def testReverseGrad(self):
    rev = lambda operand: lax.rev(operand, dimensions)

    dimensions = [0]
    check_grads(rev, (onp.array([3., 2., 1.]),), 2)

    dimensions = [0, 1]
    check_grads(rev, (onp.array([[6., 5., 4.], [3., 2., 1.]]),), 2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}".format(
          jtu.format_shape_dtype_string(pred_shape, onp.bool_),
          jtu.format_shape_dtype_string(arg_shape, dtype)),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "dtype": dtype,
       "rng": rng}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testSelectGrad(self, pred_shape, arg_shape, dtype, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    pred = rng(pred_shape, onp.bool_)
    on_true = rng(arg_shape, dtype)
    on_false = rng(arg_shape, dtype)
    select = lambda on_true, on_false: lax.select(pred, on_true, on_false)
    check_grads(select, (on_true, on_false), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides, "rng": rng}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testSliceGrad(self, shape, dtype, starts, limits, strides, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    slice = lambda x: lax.slice(x, starts, limits, strides)
    check_grads(slice, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_size_indices={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, size_indices),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "size_indices": size_indices, "rng": rng}
      for shape, start_indices, size_indices in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicSliceGrad(self, shape, dtype, start_indices, size_indices,
                           rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    dynamic_slice = lambda x: lax.dynamic_slice(x, start_indices, size_indices)
    check_grads(dynamic_slice, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_start_indices={}_update_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, update_shape),
       "shape": shape, "dtype": dtype, "start_indices": start_indices,
       "update_shape": update_shape, "rng": rng}
      for shape, start_indices, update_shape in [
        [(3,), (1,), (1,)],
        [(5, 3), (1, 1), (3, 1)],
        [(7, 5, 3), (4, 1, 0), (2, 0, 1)],
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testDynamicUpdateSliceGrad(self, shape, dtype, start_indices,
                                 update_shape, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    update = rng(update_shape, dtype)
    start_indices = onp.array(start_indices)

    dus = lambda x, y: lax.dynamic_update_slice(x, y, start_indices)
    check_grads(dus, (operand, update), 2, ["fwd", "rev"], tol, tol, tol)

    dus = lambda x: lax.dynamic_update_slice(x, update, start_indices)
    check_grads(dus, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

    dus = lambda y: lax.dynamic_update_slice(operand, y, start_indices)
    check_grads(dus, (update,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm),
       "shape": shape, "dtype": dtype, "perm": perm, "rng": rng}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for dtype in float_dtypes
      for rng in [jtu.rand_default()]))
  def testTransposeGrad(self, shape, dtype, perm, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    transpose = lambda x: lax.transpose(x, perm)
    check_grads(transpose, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_inshape={}_reducedims={}"
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype), dims),
       "op": op, "init_val": init_val, "shape": shape, "dtype": dtype,
       "dims": dims, "rng": rng}
      for init_val, op, dtypes in [
          (0, lax.add, inexact_dtypes),
          (-onp.inf, lax.max, inexact_dtypes),
          (onp.inf, lax.min, inexact_dtypes),
          (1, lax.mul, inexact_dtypes),
      ]
      for dtype in dtypes
      for shape, dims in [
          [(3, 4, 5), ()],
          [(3, 4, 5), (0,)],
          [(3, 4, 5), (1, 2)],
          [(3, 4, 5), (0, 2)],
          [(3, 4, 5), (0, 1, 2)],
          [(3, 1), (1,)],
      ]
      for rng in [jtu.rand_small()]))
  def testReduceGrad(self, op, init_val, shape, dtype, dims, rng):
    if jtu.device_under_test() == "tpu" and op is lax.mul:
      raise SkipTest("unimplemented case")
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    init_val = onp.asarray(init_val, dtype=dtype)
    reduce = lambda operand: lax.reduce(operand, init_val, op, dims)
    check_grads(reduce, (operand,), 1, ["fwd", "rev"], tol, tol)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_dtype={}_padding={}"
       .format(op.__name__, onp.dtype(dtype).name, padding),
       "op": op, "init_val": init_val, "dtype": dtype, "padding": padding,
       "rng": rng}
      for init_val, op, dtypes, rng in [
          (0, lax.add, float_dtypes, jtu.rand_small()),
          (-onp.inf, lax.max, [onp.float32], jtu.rand_default()),
          (onp.inf, lax.min, [onp.float32], jtu.rand_default()),
      ]
      for dtype in dtypes
      for padding in ["VALID", "SAME"]
      for rng in [jtu.rand_default()]))
  def testReduceWindowGrad(self, op, init_val, dtype, padding, rng):
    init_val = onp.asarray(init_val, dtype=dtype)

    # We need this conditional and the corresponding loop logic to be in the
    # test method, rather than at the parameterized test level, because it
    # depends on FLAGS for the device under test.
    # TODO(b/31565929): enable when fixed.
    if jtu.device_under_test() == "tpu" and op is not lax.add:
      all_configs = [((6, 5, 4, 3), (2, 2, 1, 1), (1, 2, 1, 1))]

      # TODO(b/73062247): need variadic reduce-window for better precision.
      gradient_order = 1
    else:
      all_configs = itertools.chain(
          itertools.product(
              [(4, 6)],  # shapes
              [(2, 1), (1, 2)],  # window_dimensions
              [(1, 1), (2, 1), (1, 2)]  # strides
          ),
          itertools.product(
              [(3, 2, 4, 6)],  # shapes
              [(1, 1, 2, 1), (2, 1, 2, 1)],  # window_dimensions
              [(1, 2, 2, 1), (1, 1, 1, 1)]),  # strides
      )
      gradient_order = 3

    def fun(operand):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding)

    for shape, dims, strides in all_configs:
      operand = rng(shape, dtype)
      if op is not lax.add:
        # this test can fail if there are duplicates in operand
        self.assertEqual(onp.unique(operand).size, operand.size,
                         msg="test requires operand elements to be unique.")
      check_grads(fun, (operand,), gradient_order, ["fwd", "rev"], 1e-2, 1e-2,
                  1e-2)

  # TODO(b/205052657): enable more tests when supported
  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, dtype), axis),
       "rng": rng, "shape": shape, "dtype": dtype, "axis": axis}
      for dtype in [onp.float32]
      for shape in [(5,), (5, 7)]
      for axis in [len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSortGrad(self, shape, dtype, axis, rng):
    tol = 1e-2 if onp.finfo(dtype).bits == 32 else None
    operand = rng(shape, dtype)
    sort = lambda x: lax.sort(x, axis)
    check_grads(sort, (operand,), 2, ["fwd", "rev"], tol, tol, tol)

  # TODO(b/205052657): enable more tests when supported
  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_keyshape={}_valshape={}_axis={}".format(
          jtu.format_shape_dtype_string(shape, key_dtype),
          jtu.format_shape_dtype_string(shape, val_dtype),
          axis),
       "rng": rng, "shape": shape,
       "key_dtype": key_dtype, "val_dtype": val_dtype, "axis": axis}
      for key_dtype in [onp.float32]
      for val_dtype in [onp.float32]
      for shape in [(3,), (5, 3)]
      for axis in [len(shape) - 1]
      for rng in [jtu.rand_default()]))
  def testSortKeyValGrad(self, shape, key_dtype, val_dtype, axis, rng):
    # This test relies on the property that wherever keys are tied, values are
    # too, since we don't guarantee the same ordering of values with equal keys.
    # To avoid that case, we generate unique keys (globally in the key array).
    perm_rng = onp.random.RandomState(0)
    def args_maker():
      flat_keys = onp.arange(onp.prod(shape, dtype=int), dtype=key_dtype)
      keys = perm_rng.permutation(flat_keys).reshape(shape)
      values = rng(shape, val_dtype)
      return keys, values
    keys, values = args_maker()

    fun = lambda keys, values: lax.sort_key_val(keys, values, axis)
    check_grads(fun, (keys, values), 2, ["fwd", "rev"], 1e-2, 1e-2, 1e-2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_axes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, axes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "axes": axes, "rng": rng}
      for dtype in float_dtypes
      for shape, idxs, axes in [
          [(3, 4, 5), (onp.array([0, 2, 1]),), (0,)],
          [(3, 4, 5), (onp.array([-1, -2]),), (0,)],
          [(3, 4, 5), (onp.array([0, 2]), onp.array([1, 3])), (0, 1)],
          [(3, 4, 5), (onp.array([0, 2]), onp.array([1, 3])), (0, 2)],
      ]
      for rng in [jtu.rand_default()]))
  def testIndexTakeGrad(self, shape, dtype, idxs, axes, rng):
    src = rng(shape, dtype)
    index_take = lambda src: lax.index_take(src, idxs, axes)
    check_grads(index_take, (src,), 2, ["fwd", "rev"], 1e-2, 1e-2, 1)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_dnums={}_slice_sizes={}".format(
          jtu.format_shape_dtype_string(shape, dtype), idxs, dnums,
          slice_sizes),
       "shape": shape, "dtype": dtype, "idxs": idxs, "dnums": dnums,
       "slice_sizes": slice_sizes, "rng": rng, "rng_idx": rng_idx}
      for dtype in float_dtypes
      for shape, idxs, dnums, slice_sizes in [
          ((5,), onp.array([[0], [2]]), lax.GatherDimensionNumbers(
            offset_dims=(), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1,)),
          ((10,), onp.array([[0], [0], [0]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(), start_index_map=(0,)),
            (2,)),
          ((10, 5,), onp.array([[0], [2], [1]]), lax.GatherDimensionNumbers(
            offset_dims=(1,), collapsed_slice_dims=(0,), start_index_map=(0,)),
            (1, 3)),
      ]
      for rng_idx in [jtu.rand_int(max(shape))]
      for rng in [jtu.rand_default()]))
  def testGatherGrad(self, shape, dtype, idxs, dnums, slice_sizes, rng, rng_idx):
    idxs = rng_idx(idxs.shape, idxs.dtype)
    gather = lambda x: lax.gather(x, idxs, dimension_numbers=dnums,
                                  slice_sizes=slice_sizes)
    x = rng(shape, dtype)
    check_grads(gather, (x,), 2, ["fwd", "rev"], 1e-2, 1e-2, 1.)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatterAddGrad(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                         rng_idx):
    idxs = rng_idx(idxs.shape, idxs.dtype)
    scatter_add = lambda x, y: lax.scatter_add(x, idxs, y,
                                               dimension_numbers=dnums)
    x = rng(arg_shape, dtype)
    y = rng(update_shape, dtype)
    check_grads(scatter_add, (x, y), 2, ["fwd", "rev"], 1e-2, 1e-2, 1.)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_idxs={}_update={}_dnums={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          idxs, update_shape, dnums),
       "arg_shape": arg_shape, "dtype": dtype, "idxs": idxs,
       "update_shape": update_shape, "dnums": dnums, "rng": rng,
       "rng_idx": rng_idx}
      for dtype in float_dtypes
      for arg_shape, idxs, update_shape, dnums in [
          ((5,), onp.array([[0], [2]]), (2,), lax.ScatterDimensionNumbers(
            update_window_dims=(), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
          ((10,), onp.array([[0], [0], [0]]), (3, 2), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(),
            scatter_dims_to_operand_dims=(0,))),
          ((10, 5,), onp.array([[0], [2], [1]]), (3, 3), lax.ScatterDimensionNumbers(
            update_window_dims=(1,), inserted_window_dims=(0,),
            scatter_dims_to_operand_dims=(0,))),
      ]
      for rng_idx in [jtu.rand_int(max(arg_shape))]
      for rng in [jtu.rand_default()]))
  def testScatterGrad(self, arg_shape, dtype, idxs, update_shape, dnums, rng,
                         rng_idx):
    idxs = rng_idx(idxs.shape, idxs.dtype)
    scatter = lambda x, y: lax.scatter(x, idxs, y, dimension_numbers=dnums)
    x = rng(arg_shape, dtype)
    y = rng(update_shape, dtype)
    check_grads(scatter, (x, y), 2, ["fwd", "rev"], 1e-2, 1e-2, 1.)

  def testStopGradient(self):
    def f(x):
      return lax.sin(x) * lax.cos(lax.stop_gradient(x))

    def f2(x, y):
      return lax.sin(x) * lax.cos(y)

    x = 3.14
    ans = api.grad(f)(x)
    expected = api.grad(f2)(x, x)
    self.assertAllClose(ans, expected, check_dtypes=True)

    ans = api.grad(api.grad(f))(x)
    expected = api.grad(api.grad(f2))(x, x)
    self.assertAllClose(ans, expected, check_dtypes=True)

    ans = api.grad(lambda x: lax.stop_gradient({'foo':x})['foo'])(3.)
    expected = onp.array(0.0)
    self.assertAllClose(ans, expected, check_dtypes=False)

  # TODO(mattjj): make this a more systematic test
  def testRemainder(self):
    rng = onp.random.RandomState(0)
    x = rng.uniform(-0.9, 9, size=(3, 4))
    y = rng.uniform(0.7, 1.9, size=(3, 1))
    assert not set(onp.unique(x)) & set(onp.unique(y))
    tol = 1e-1 if num_float_bits(onp.float64) == 32 else 1e-3
    check_grads(lax.rem, (x, y), 2, ["fwd", "rev"], tol, tol)

    rng = onp.random.RandomState(0)
    x = rng.uniform(-0.9, 9, size=(1, 4))
    y = rng.uniform(0.7, 1.9, size=(3, 4))
    assert not set(onp.unique(x)) & set(onp.unique(y))
    tol = 1e-1 if num_float_bits(onp.float64) == 32 else 1e-3
    check_grads(lax.rem, (x, y), 2, ["fwd", "rev"], tol, tol)


def all_bdims(*shapes):
  bdims = (itertools.chain([None], range(len(shape) + 1)) for shape in shapes)
  return (t for t in itertools.product(*bdims) if not all(e is None for e in t))

def add_bdim(bdim_size, bdim, shape):
  shape = list(shape)
  if bdim is not None:
    shape.insert(bdim, bdim_size)
  return tuple(shape)

def slicer(x, bdim):
  if bdim is None:
    return lambda _: x
  else:
    return lambda i: lax.index_in_dim(x, i, bdim, keepdims=False)

def args_slicer(args, bdims):
  slicers = list(map(slicer, args, bdims))
  return lambda i: [sl(i) for sl in slicers]

class LaxVmapTest(jtu.JaxTestCase):

  def _CheckBatching(self, op, bdim_size, bdims, shapes, dtype, rng,
                     rtol=None, atol=None):
    batched_shapes = map(partial(add_bdim, bdim_size), bdims, shapes)
    args = [rng(shape, dtype) for shape in batched_shapes]
    args_slice = args_slicer(args, bdims)
    ans = api.vmap(op, bdims)(*args)
    expected = onp.stack([op(*args_slice(i)) for i in range(bdim_size)])
    self.assertAllClose(ans, expected, check_dtypes=True, rtol=rtol, atol=atol)

  @parameterized.named_parameters(itertools.chain.from_iterable(
      jtu.cases_from_list(
        {"testcase_name": "{}_bdims={}".format(
            jtu.format_test_name_suffix(rec.op.__name__, shapes,
                                        itertools.repeat(dtype)), bdims),
         "op": rec.op, "rng": rec.rng, "shapes": shapes, "dtype": dtype,
         "bdims": bdims}
        for shape_group in compatible_shapes
        for shapes in CombosWithReplacement(shape_group, rec.nargs)
        for bdims in all_bdims(*shapes)
        for dtype in rec.dtypes)
      for rec in LAX_OPS))
  def testOp(self, op, rng, shapes, dtype, bdims):
    self._CheckBatching(op, 10, bdims, shapes, dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_strides={}_padding={}_lhs_dilation={}_"
       "rhs_dilation={}_dims={}_feature_group_count={}_lhs_bdim={}_rhs_bdim={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               strides, padding, lhs_dil, rhs_dil, ",".join(dim_nums),
               feature_group_count, lhs_bdim, rhs_bdim),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "strides": strides, "padding": padding, "lhs_dil": lhs_dil,
       "rhs_dil": rhs_dil, "rng": rng, "dimension_numbers": dim_nums,
       "perms": perms, "lhs_bdim": lhs_bdim, "rhs_bdim": rhs_bdim,
       "feature_group_count": feature_group_count}
      for lhs_shape, rhs_shape, all_strides, all_pads, lhs_dils, rhs_dils in [
          ((b, i, 6, 7),  # lhs_shape
           (j, i, 1, 2),  # rhs_shape
           [(1, 1), (1, 2), (2, 1)],  # strides
           [((0, 0), (0, 0)), ((1, 0), (0, 1)), ((0, -1), (0, 0))],  # pads
           [(1, 1), (2, 1)],  # lhs_dils
           [(1, 1), (2, 2)])  # rhs_dils
          for b, i, j in itertools.product([1, 2], repeat=3)]
      for feature_group_count in [1, 2]
      for strides in all_strides
      for rhs_dil in rhs_dils
      for lhs_dil in lhs_dils
      for dtype in [onp.float32]
      for padding in all_pads
      for dim_nums, perms in [
          (("NCHW", "OIHW", "NCHW"), ([0, 1, 2, 3], [0, 1, 2, 3])),
          (("NHWC", "HWIO", "NHWC"), ([0, 2, 3, 1], [2, 3, 1, 0])),
          (("NHWC", "OIHW", "NCHW"), ([0, 2, 3, 1], [0, 1, 2, 3]))]
      for lhs_bdim in itertools.chain([None], range(len(lhs_shape) + 1))
      for rhs_bdim in itertools.chain([None], range(len(rhs_shape) + 1))
      if (lhs_bdim, rhs_bdim) != (None, None)
      for rng in [jtu.rand_default()]
  ))
  # TODO(mattjj): some cases fail on TPU just due to numerical tolerances
  @jtu.skip_on_devices("tpu")
  def testConvGeneralDilatedBatching(
      self, lhs_shape, rhs_shape, dtype, strides, padding, lhs_dil, rhs_dil,
      dimension_numbers, perms, feature_group_count, lhs_bdim, rhs_bdim, rng):
    tol = 1e-1 if onp.finfo(dtype).bits == 32 else 1e-3

    # permute shapes to match dim_spec, scale by feature_group_count
    lhs_perm, rhs_perm = perms
    lhs_shape = list(onp.take(lhs_shape, lhs_perm))
    rhs_shape = list(onp.take(rhs_shape, rhs_perm))
    dim_spec = lax.conv_dimension_numbers(lhs_shape, rhs_shape, dimension_numbers)
    lhs_shape[dim_spec.lhs_spec[1]] *= feature_group_count
    rhs_shape[dim_spec.rhs_spec[0]] *= feature_group_count

    conv = partial(lax.conv_general_dilated, window_strides=strides,
                   padding=padding, lhs_dilation=lhs_dil, rhs_dilation=rhs_dil,
                   dimension_numbers=dimension_numbers,
                   feature_group_count=feature_group_count,
                   precision=lax.Precision.HIGHEST)
    self._CheckBatching(conv, 5, (lhs_bdim, rhs_bdim), (lhs_shape, rhs_shape),
                        dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_from_dtype={}_to_dtype={}_bdims={}".format(
          shape, from_dtype, to_dtype, bdims),
       "shape": shape, "from_dtype": from_dtype, "to_dtype": to_dtype,
       "bdims": bdims, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for shape in [(2, 3)]
      for bdims in all_bdims(shape)
      for rng in [jtu.rand_default()]))
  def testConvertElementType(self, shape, from_dtype, to_dtype, bdims, rng):
    op = lambda x: lax.convert_element_type(x, to_dtype)
    self._CheckBatching(op, 10, bdims, (shape,), from_dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_from_dtype={}_to_dtype={}_bdims={}".format(
          shape, from_dtype, to_dtype, bdims),
       "shape": shape, "from_dtype": from_dtype, "to_dtype": to_dtype,
       "bdims": bdims, "rng": rng}
      for from_dtype, to_dtype in itertools.product(
          [onp.float32, onp.int32, "float32", "int32"], repeat=2)
      for shape in [(2, 3)]
      for bdims in all_bdims(shape)
      for rng in [jtu.rand_default()]))
  def testBitcastElementType(self, shape, from_dtype, to_dtype, bdims, rng):
    op = lambda x: lax.bitcast_convert_type(x, to_dtype)
    self._CheckBatching(op, 10, bdims, (shape,), from_dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_min_shape={}_operand_shape={}_max_shape={}_bdims={}"
       .format(jtu.format_shape_dtype_string(min_shape, dtype),
               jtu.format_shape_dtype_string(operand_shape, dtype),
               jtu.format_shape_dtype_string(max_shape, dtype),
               bdims),
       "min_shape": min_shape, "operand_shape": operand_shape,
       "max_shape": max_shape, "dtype": dtype, "bdims": bdims, "rng": rng}
      for min_shape, operand_shape, max_shape in [
          [(), (2, 3), ()],
          [(2, 3), (2, 3), ()],
          [(), (2, 3), (2, 3)],
          [(2, 3), (2, 3), (2, 3)],
      ]
      for dtype in default_dtypes
      for bdims in all_bdims(min_shape, operand_shape, max_shape)
      for rng in [jtu.rand_default()]))
  def testClamp(self, min_shape, operand_shape, max_shape, dtype, bdims, rng):
    raise SkipTest("batching rule for clamp not implemented")  # TODO(mattj)
    shapes = [min_shape, operand_shape, max_shape]
    self._CheckBatching(lax.clamp, 10, bdims, shapes, dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_lhs_shape={}_rhs_shape={}_bdims={}".format(
          jtu.format_shape_dtype_string(lhs_shape, dtype),
          jtu.format_shape_dtype_string(rhs_shape, dtype),
          bdims),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "bdims": bdims, "rng": rng}
      for lhs_shape in [(3,), (4, 3)] for rhs_shape in [(3,), (3, 6)]
      for bdims in all_bdims(lhs_shape, rhs_shape)
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testDot(self, lhs_shape, rhs_shape, dtype, bdims, rng):
    self._CheckBatching(lax.dot, 5, bdims, (lhs_shape, rhs_shape), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_lhs_contracting={}_rhs_contracting={}_bdims={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               lhs_contracting, rhs_contracting, bdims),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "lhs_contracting": lhs_contracting, "rhs_contracting": rhs_contracting,
       "bdims": bdims, "rng": rng}
      for lhs_shape, rhs_shape, lhs_contracting, rhs_contracting in [
          [(3, 5), (2, 5), [1], [1]],
          [(5, 3), (5, 2), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0], [0]],
          [(5, 3, 2), (5, 2, 4), [0,2], [0,1]],
          [(1, 2, 2, 3), (1, 2, 3, 1), [1], [1]],
          [(3, 2), (2, 4), [1], [0]],
      ]
      for bdims in all_bdims(lhs_shape, rhs_shape)
      for dtype in default_dtypes
      for rng in [jtu.rand_small()]))
  def testDotGeneralContractOnly(self, lhs_shape, rhs_shape, dtype,
                                 lhs_contracting, rhs_contracting, bdims, rng):
    dimension_numbers = ((lhs_contracting, rhs_contracting), ([], []))
    dot = partial(lax.dot_general, dimension_numbers=dimension_numbers)
    self._CheckBatching(dot, 5, bdims, (lhs_shape, rhs_shape), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs_shape={}_rhs_shape={}_dimension_numbers={}_bdims={}"
       .format(jtu.format_shape_dtype_string(lhs_shape, dtype),
               jtu.format_shape_dtype_string(rhs_shape, dtype),
               dimension_numbers, bdims),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "dimension_numbers": dimension_numbers, "bdims": bdims, "rng": rng}
      for lhs_shape, rhs_shape, dimension_numbers in [
          ((3, 3, 2), (3, 2, 4), (([2], [1]), ([0], [0]))),
          ((3, 4, 2, 4), (3, 4, 3, 2), (([2], [3]), ([0, 1], [0, 1]))),
      ]
      for bdims in all_bdims(lhs_shape, rhs_shape)
      for dtype in default_dtypes
      for rng in [jtu.rand_small()]))
  def testDotGeneralContractAndBatch(self, lhs_shape, rhs_shape, dtype,
                                     dimension_numbers, bdims, rng):
    dot = partial(lax.dot_general, dimension_numbers=dimension_numbers)
    self._CheckBatching(dot, 5, bdims, (lhs_shape, rhs_shape), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_dtype={}_broadcast_sizes={}_bdims={}".format(
          shape, onp.dtype(dtype).name, broadcast_sizes, bdims),
       "shape": shape, "dtype": dtype, "broadcast_sizes": broadcast_sizes,
       "bdims": bdims, "rng": rng}
      for shape in [(), (2, 3)]
      for dtype in default_dtypes
      for broadcast_sizes in [(), (2,), (1, 2)]
      for bdims in all_bdims(shape)
      for rng in [jtu.rand_default()]))
  def testBroadcast(self, shape, dtype, broadcast_sizes, bdims, rng):
    op = lambda x: lax.broadcast(x, broadcast_sizes)
    self._CheckBatching(op, 5, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_bcdims={}_bdims={}".format(
          jtu.format_shape_dtype_string(inshape, dtype),
          outshape, broadcast_dimensions, bdims),
       "inshape": inshape, "dtype": dtype, "outshape": outshape,
       "dimensions": broadcast_dimensions, "bdims": bdims, "rng": rng}
      for inshape, outshape, broadcast_dimensions in [
          ([2], [2, 2], [0]),
          ([2], [2, 2], [1]),
          ([2], [2, 3], [0]),
          ([], [2, 3], []),
      ]
      for dtype in default_dtypes
      for bdims in all_bdims(inshape)
      for rng in [jtu.rand_default()]))
  def testBroadcastInDim(self, inshape, dtype, outshape, dimensions, bdims, rng):
    raise SkipTest("this test has failures in some cases")  # TODO(mattjj)
    op = lambda x: lax.broadcast_in_dim(x, outshape, dimensions)
    self._CheckBatching(op, 5, bdims, (inshape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_outshape={}_dims={}_bdims={}".format(
          jtu.format_shape_dtype_string(arg_shape, dtype),
          jtu.format_shape_dtype_string(out_shape, dtype),
          dimensions, bdims),
       "arg_shape": arg_shape, "out_shape": out_shape, "dtype": dtype,
       "dimensions": dimensions, "bdims": bdims, "rng": rng}
      for dtype in default_dtypes
      for arg_shape, dimensions, out_shape in [
          [(3, 4), None, (12,)],
          [(2, 1, 4), None, (8,)],
          [(2, 2, 4), None, (2, 8)],
          [(2, 2, 4), (0, 1, 2), (2, 8)],
          [(2, 2, 4), (1, 0, 2), (8, 2)],
          [(2, 2, 4), (2, 1, 0), (4, 2, 2)]
      ]
      for bdims in all_bdims(arg_shape)
      for rng in [jtu.rand_default()]))
  def testReshape(self, arg_shape, out_shape, dtype, dimensions, bdims, rng):
    op = lambda x: lax.reshape(x, out_shape, dimensions=dimensions)
    self._CheckBatching(op, 10, bdims, (arg_shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inshape={}_pads={}_bdims={}"
       .format(jtu.format_shape_dtype_string(shape, dtype), pads, bdims),
       "shape": shape, "dtype": dtype, "pads": pads, "rng": jtu.rand_small(),
       "bdims": bdims}
      for shape in [(2, 3)]
      for bdims in all_bdims(shape)
      for dtype in default_dtypes
      for pads in [[(1, 2, 1), (0, 1, 0)]]))
  def testPad(self, shape, dtype, pads, bdims, rng):
    fun = lambda operand: lax.pad(operand, onp.array(0, dtype), pads)
    self._CheckBatching(fun, 5, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_predshape={}_argshapes={}_bdims={}".format(
          jtu.format_shape_dtype_string(pred_shape, onp.bool_),
          jtu.format_shape_dtype_string(arg_shape, arg_dtype),
          bdims),
       "pred_shape": pred_shape, "arg_shape": arg_shape, "arg_dtype": arg_dtype,
       "bdims": bdims, "rng": rng}
      for arg_shape in [(), (3,), (2, 3)]
      for pred_shape in ([(), arg_shape] if arg_shape else [()])
      for bdims in all_bdims(pred_shape, arg_shape, arg_shape)
      for arg_dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSelect(self, pred_shape, arg_shape, arg_dtype, bdims, rng):
    op = lambda c, x, y: lax.select(c < 0, x, y)
    self._CheckBatching(op, 5, bdims, (pred_shape, arg_shape, arg_shape,),
                        arg_dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_start_indices={}_limit_indices={}_strides={}_bdims={}".format(
          jtu.format_shape_dtype_string(shape, dtype),
          start_indices, limit_indices, strides, bdims),
       "shape": shape, "dtype": dtype, "starts": start_indices,
       "limits": limit_indices, "strides": strides, "bdims": bdims, "rng": rng}
      for shape, start_indices, limit_indices, strides in [
        [(3,), (1,), (2,), None],
        [(7,), (4,), (7,), None],
        [(5,), (1,), (5,), (2,)],
        [(8,), (1,), (6,), (2,)],
        [(5, 3), (1, 1), (3, 2), None],
        [(5, 3), (1, 1), (3, 1), None],
        [(7, 5, 3), (4, 0, 1), (7, 1, 3), None],
        [(5, 3), (1, 1), (2, 1), (1, 1)],
        [(5, 3), (1, 1), (5, 3), (2, 1)],
      ]
      for bdims in all_bdims(shape)
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testSlice(self, shape, dtype, starts, limits, strides, bdims, rng):
    op = lambda x: lax.slice(x, starts, limits, strides)
    self._CheckBatching(op, 5, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_perm={}_bdims={}".format(
          jtu.format_shape_dtype_string(shape, dtype), perm, bdims),
       "shape": shape, "dtype": dtype, "perm": perm, "bdims": bdims, "rng": rng}
      for shape, perm in [
        [(3, 4), (1, 0)],
        [(3, 4), (0, 1)],
        [(3, 4, 5), (2, 1, 0)],
        [(3, 4, 5), (1, 0, 2)],
      ]
      for bdims in all_bdims(shape)
      for dtype in default_dtypes
      for rng in [jtu.rand_default()]))
  def testTranspose(self, shape, dtype, perm, bdims, rng):
    op = lambda x: lax.transpose(x, perm)
    self._CheckBatching(op, 5, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_inshape={}_reducedims={}_initval={}_bdims={}"
       .format(op.__name__, jtu.format_shape_dtype_string(shape, dtype), dims,
               init_val, bdims),
       "op": op, "init_val": init_val, "shape": shape, "dtype": dtype,
       "dims": dims, "bdims": bdims, "rng": rng}
      for init_val, op, dtypes in [
          (0, lax.add, default_dtypes),
          (1, lax.mul, default_dtypes),
          (0, lax.max, all_dtypes), # non-monoidal
          (-onp.inf, lax.max, float_dtypes),
          (onp.iinfo(onp.int32).min, lax.max, [onp.int32]),
          (onp.iinfo(onp.int64).min, lax.max, [onp.int64]),
          (onp.iinfo(onp.uint32).min, lax.max, [onp.uint32]),
          (onp.iinfo(onp.uint64).min, lax.max, [onp.uint64]),
          (onp.inf, lax.min, float_dtypes),
          (onp.iinfo(onp.int32).max, lax.min, [onp.int32]),
          (onp.iinfo(onp.int64).max, lax.min, [onp.int64]),
          (onp.iinfo(onp.uint32).max, lax.min, [onp.uint32]),
          (onp.iinfo(onp.uint64).max, lax.min, [onp.uint64]),
      ]
      for dtype in dtypes
      for shape, dims in [
          [(3, 4, 5), (0,)], [(3, 4, 5), (1, 2)],
          [(3, 4, 5), (0, 2)], [(3, 4, 5), (0, 1, 2)]
      ]
      for bdims in all_bdims(shape)
      for rng in [jtu.rand_small()]))
  def testReduce(self, op, init_val, shape, dtype, dims, bdims, rng):
    init_val = onp.asarray(init_val, dtype=dtype)
    fun = lambda operand: lax.reduce(operand, init_val, op, dims)
    self._CheckBatching(fun, 5, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_op={}_dtype={}_padding={}"
       .format(op.__name__, onp.dtype(dtype).name, padding),
       "op": op, "init_val": init_val, "dtype": dtype, "padding": padding,
       "rng": rng}
      for init_val, op, dtypes in [
          (0, lax.add, [onp.float32]),
          (-onp.inf, lax.max, [onp.float32]),
          (onp.inf, lax.min, [onp.float32]),
      ]
      for dtype in dtypes
      for padding in ["VALID", "SAME"]
      for rng in [jtu.rand_small()]))
  def testReduceWindow(self, op, init_val, dtype, padding, rng):
    init_val = onp.asarray(init_val, dtype=dtype)

    all_configs = itertools.chain(
        itertools.product(
            [(4, 6)],
            [(2, 1), (1, 2)],
            [(1, 1), (2, 1), (1, 2)]),
        itertools.product(
            [(3, 2, 4, 6)], [(1, 1, 2, 1), (2, 1, 2, 1)],
            [(1, 2, 2, 1), (1, 1, 1, 1)]))

    def fun(operand):
      return lax.reduce_window(operand, init_val, op, dims, strides, padding)

    for shape, dims, strides in all_configs:
      for bdims in all_bdims(shape):
        self._CheckBatching(fun, 3, bdims, (shape,), dtype, rng)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_bdims={}_fft_ndims={}"
       .format(shape, bdims, fft_ndims),
       "shape": shape, "bdims": bdims, "fft_ndims": fft_ndims, "rng": rng}
      for shape in [(5,), (3, 4, 5), (2, 3, 4, 5)]
      for bdims in all_bdims(shape)
      for fft_ndims in range(0, min(3, len(shape)) + 1)
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")  # TODO(b/137993701): unimplemented cases.
  def testFft(self, fft_ndims, shape, bdims, rng):
    ndims = len(shape)
    axes = range(ndims - fft_ndims, ndims)
    fft_lengths = [shape[axis] for axis in axes]
    op = lambda x: lax.fft(x, xla_client.FftType.FFT, fft_lengths)
    self._CheckBatching(op, 5, bdims, [shape], onp.complex64, rng)

  # TODO Concatenate
  # TODO Reverse
  # TODO DynamicSlice
  # TODO DynamicUpdateSlice
  # TODO Sort
  # TODO SortKeyVal
  # TODO Collapse
  # TODO ScatterAdd
  # TODO Scatter


if __name__ == '__main__':
  absltest.main()
