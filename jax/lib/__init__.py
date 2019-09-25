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

# This module is largely a wrapper around `jaxlib` that performs version
# checking on import.

import jaxlib

_minimum_jaxlib_version = (0, 1, 28)
try:
  from jaxlib import version as jaxlib_version
except:
  # jaxlib is too old to have version number.
  msg = 'This version of jax requires jaxlib version >= {}.'
  raise ImportError(msg.format('.'.join(map(str, _minimum_jaxlib_version))))

version = tuple(int(x) for x in jaxlib_version.__version__.split('.'))

# Check the jaxlib version before importing anything else from jaxlib.
def _check_jaxlib_version():
  if version < _minimum_jaxlib_version:
    msg = 'jaxlib is version {}, but this version of jax requires version {}.'
    raise ValueError(msg.format('.'.join(map(str, version)),
                                '.'.join(map(str, _minimum_jaxlib_version))))

_check_jaxlib_version()


from jaxlib import xla_client
from jaxlib import xrt
from jaxlib import lapack

from jaxlib import pytree
from jaxlib import cusolver
