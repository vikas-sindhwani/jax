# Copyright 2019 Google LLC
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

"""Shared neural network activations and other functions."""

from __future__ import absolute_import
from __future__ import division

import numpy as onp

from jax import lax
from jax import random
from jax.scipy.special import expit
import jax.numpy as np
from jax import jarrett

# activations

def relu(x): return np.maximum(x, 0)
def softplus(x): return np.log1p(np.exp(x))
def soft_sign(x): return x / (np.abs(x) + 1)
def sigmoid(x): return expit(x)
def swish(x): return x * sigmoid(x)
def log_sigmoid(x): return -softplus(-x)

def elu(x, alpha=1.0):
  return np.where(x > 0, x, alpha * np.expm1(x))

def leaky_relu(x, negative_slope=1e-2):
  return np.where(x >= 0, x, negative_slope * x)

def hard_tanh(x):
  return np.where(x > 1, 1, np.where(x < -1, -1, x))

def celu(x, alpha=1.0):
  """Continuously-differentiable exponential linear unit activation"""
  return np.where(x > 0, x, alpha * np.expm1(x / alpha))

def selu(x):
  """Scaled exponential linear unit activation"""
  alpha = 1.6732632423543772848170429916717
  scale = 1.0507009873554804934193349852946
  return scale * leaky_relu(x, alpha)

@jarrett
def gelu(x):
  """Gaussian error linear unit activation"""
  return x * (lax.erf(x / np.sqrt(2)) + 1) / 2

def glu(x, axis=-1):
  """Gated linear unit activation"""
  size = x.shape[axis]
  assert size % 2 == 0, "axis size must be divisible by 2"
  return x[..., :size] * sigmoid(x[..., size:])

# other functions

def log_softmax(x, axis=-1):
  shifted = x - x.max(axis, keepdims=True)
  return shifted - np.log(np.sum(np.exp(shifted), axis, keepdims=True))

def softmax(x, axis=-1):
  unnormalized = np.exp(x - x.max(axis, keepdims=True))
  return unnormalized / unnormalized.sum(axis, keepdims=True)

def normalize(x, axis=-1, mean=None, variance=None, epsilon=1e-5):
  """Normalize an array by subtracting mean and dividing by sqrt(var)."""
  if mean is None:
    mean = np.mean(x, axis, keepdims=True)
  if variance is None:
    # this definition is traditionally seen as less accurate than np.var's
    # mean((x - mean(x))**2) but may be faster and even, given typical
    # activation distributions and low-precision arithmetic, more accurate
    # when used in neural network normalization layers
    variance = np.mean(x**2, axis, keepdims=True) - mean**2
  return (x - mean) * lax.rsqrt(variance + epsilon)
