# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This subpackage defines functions for interfacing devices' batch execution
capabilities with different machine learning libraries.
"""
# pylint: disable=import-outside-toplevel)
from functools import partial

import pennylane as qml

from .autograd import execute as execute_autograd


from collections import OrderedDict


def tape_hash(tape):
    fingerprint = []
    fingerprint.extend(
        (
            str(op.name),
            tuple(op.wires.tolist()),
            str(op.data),
        )
        for op in tape.operations
    )
    fingerprint.extend(
        (str(op.name), tuple(op.wires.tolist()), str(op.data), op.return_type)
        for op in tape.measurements
    )
    fingerprint = tuple(item for sublist in fingerprint for item in sublist)
    return hash(fingerprint)


def execute_fn_wrapper(tapes, device, **kwargs):
    cache = kwargs.pop("cache", None)

    if cache is None:
        return device.batch_execute(tapes), []

    execution_tapes = OrderedDict()
    cached_results = {}
    hashes = {}

    for i, tape in enumerate(tapes):
        hashes[i] = tape_hash(tape)

        if hashes[i] in cache:
            cached_results[i] = cache[hashes[i]]
        else:
            execution_tapes[i] = tape

    res = device.batch_execute(execution_tapes.values())
    final_res = []

    for i, tape in enumerate(tapes):
        if i in cached_results:
            final_res.append(cached_results[i])
        else:
            r = res.pop(0)
            final_res.append(r)
            cache[hashes[i]] = r

    return final_res, []


def execute(tapes, device, gradient_fn, interface="autograd", mode="best", gradient_kwargs=None):
    """Execute a batch of tapes on a device in an autodifferentiable-compatible manner.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        gradient_fn (None or callable): The gradient transform function to use
            for backward passes. If "device", the device will be queried directly
            for the gradient (if supported).
        interface (str): The interface that will be used for classical autodifferentiation.
            This affects the types of parameters that can exist on the input tapes.
            Available options include ``autograd``, ``torch``, ``tf``, and ``jax``.
        mode (str): Whether the gradients should be computed on the forward
            pass (``forward``) or the backward pass (``backward``). Only applies
            if the device is queried for the gradient; gradient transform
            functions available in ``qml.gradients`` are only supported on the backward
            pass.
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes

    Returns:
        list[list[float]]: A nested list of tape results. Each element in
        the returned list corresponds in order to the provided tapes.

    **Example**

    Consider the following cost function:

    .. code-block:: python

        dev = qml.device("lightning.qubit", wires=2)

        def cost_fn(params, x):
            with qml.tape.JacobianTape() as tape1:
                qml.RX(params[0], wires=0)
                qml.RY(params[1], wires=0)
                qml.expval(qml.PauliZ(0))

            with qml.tape.JacobianTape() as tape2:
                qml.RX(params[2], wires=0)
                qml.RY(x[0], wires=1)
                qml.CNOT(wires=[0, 1])
                qml.probs(wires=0)

            tapes = [tape1, tape2]

            # execute both tapes in a batch on the given device
            res = execute(tapes, dev, qml.gradients.param_shift)

            return res[0][0] + res[1][0, 0] - res[1][0, 1]

    In this cost function, two **independent** quantum tapes are being
    constructed; one returning an expectation value, the other probabilities.
    We then batch execute the two tapes, and reduce the results to obtain
    a scalar.

    Let's execute this cost function while tracking the gradient:

    >>> params = np.array([0.1, 0.2, 0.3], requires_grad=True)
    >>> x = np.array([0.5], requires_grad=True)
    >>> cost_fn(params, x)
    1.9305068163274222

    Since the ``execute`` function is differentiable, we can
    also compute the gradient:

    >>> qml.grad(cost_fn)(params, x)
    (array([-0.0978434 , -0.19767681, -0.29552021]), array([5.37764278e-17]))

    Finally, we can also compute any nth-order derivative. Let's compute the Jacobian
    of the gradient (that is, the Hessian):

    >>> x.requires_grad = False
    >>> qml.jacobian(qml.grad(cost_fn))(params, x)
    array([[-0.97517033,  0.01983384,  0.        ],
           [ 0.01983384, -0.97517033,  0.        ],
           [ 0.        ,  0.        , -0.95533649]])
    """
    gradient_kwargs = gradient_kwargs or {}

    if gradient_fn == "device":
        # gradient function is a device method

        if mode in ("forward", "best"):
            # replace the forward execution function to return
            # both results and gradients
            execute_fn = device.execute_and_gradients
            gradient_fn = None

        elif mode == "backward":
            # replace the backward gradient computation
            execute_fn = lambda tapes, **kwargs: (device.batch_execute(tapes), [])
            gradient_fn = device.gradients

    elif mode == "forward":
        raise ValueError("Gradient transforms cannot be used with mode='forward'")

    else:
        # gradient function is a transform
        gradient_kwargs["cache"] = {}
        execute_fn = lambda tapes, **kwargs: execute_fn_wrapper(tapes, device, **kwargs)

    if interface == "autograd":
        res = execute_autograd(tuple(tapes), device, execute_fn, gradient_fn, gradient_kwargs)
    else:
        raise ValueError(f"Unknown interface {interface}")

    if "cache" in gradient_kwargs:
        # clear the cache
        gradient_kwargs["cache"] = {}

    return res
