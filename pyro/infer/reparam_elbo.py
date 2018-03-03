from __future__ import absolute_import, division, print_function

import numbers
import warnings
import torch

import numpy as np

from pdb import set_trace as bb
import pyro
import pyro.poutine as poutine
from pyro.distributions.util import torch_zeros_like
from pyro.infer.enum import iter_discrete_traces
from pyro.infer.util import torch_backward, torch_data_sum, torch_sum
from pyro.poutine.util import prune_subsample_sites
from pyro.util import check_model_guide_match


def check_enum_discrete_can_run(model_trace, guide_trace):
    """
    Checks whether `enum_discrete` is supported for the given (model, guide) pair.

    :param Trace model: A model trace.
    :param Trace guide: A guide trace.
    :raises: NotImplementedError
    """
    # Check that all batch_log_pdf shapes are the same,
    # since we currently do not correctly handle broadcasting.
    model_trace.compute_batch_log_pdf()
    guide_trace.compute_batch_log_pdf()
    shapes = {}
    for source, trace in [("model", model_trace), ("guide", guide_trace)]:
        for name, site in trace.nodes.items():
            if site["type"] == "sample":
                shapes[site["batch_log_pdf"].size()] = (source, name)
    if len(shapes) > 1:
        raise NotImplementedError(
                "enum_discrete does not support mixture of batched and un-batched variables. "
                "Try rewriting your model to avoid batching or running with enum_discrete=False. "
                "Found the following variables of different batch shapes:\n{}".format(
                    "\n".join(["{} {}: shape = {}".format(source, name, tuple(shape))
                               for shape, (source, name) in sorted(shapes.items())])))


class Reparam_ELBO(object):
    """
    A trace implementation of ELBO-based SVI with local reparameterization
    """
    def __init__(self,
                 num_particles=1,
                 enum_discrete=False):
        """
        :param num_particles: the number of particles/samples used to form the ELBO (gradient) estimators
        :param bool enum_discrete: whether to sum over discrete latent variables, rather than sample them
        """
        super(Reparam_ELBO, self).__init__()
        self.num_particles = num_particles
        self.enum_discrete = enum_discrete

    def _get_traces(self, model, guide, *args, **kwargs):
        """
        runs the guide and runs the model against the guide with
        the result packaged as a trace generator

        XXX support for automatically settings args/kwargs to volatile?
        """

        for i in range(self.num_particles):
            if self.enum_discrete:
                # This iterates over a bag of traces, for each particle.
                for scale, guide_trace in iter_discrete_traces("flat", guide, *args, **kwargs):
                    model_trace = poutine.trace(poutine.replay(model, guide_trace),
                                                graph_type="flat").get_trace(*args, **kwargs)

                    check_model_guide_match(model_trace, guide_trace)
                    guide_trace = prune_subsample_sites(guide_trace)
                    model_trace = prune_subsample_sites(model_trace)
                    check_enum_discrete_can_run(model_trace, guide_trace)

                    log_r = model_trace.batch_log_pdf() - guide_trace.batch_log_pdf()
                    weight = scale / self.num_particles
                    yield weight, model_trace, guide_trace, log_r
                continue

            guide_trace = poutine.trace(guide).get_trace(*args, **kwargs)
            model_trace = poutine.trace(poutine.replay(model, guide_trace)).get_trace(*args, **kwargs)

            check_model_guide_match(model_trace, guide_trace)
            guide_trace = prune_subsample_sites(guide_trace)
            model_trace = prune_subsample_sites(model_trace)

            log_r = model_trace.log_pdf() - guide_trace.log_pdf()
            weight = 1.0 / self.num_particles
            yield weight, model_trace, guide_trace, log_r

    def loss(self, model, guide, *args, **kwargs):
        """
        :returns: returns an estimate of the ELBO
        :rtype: float

        Evaluates the ELBO with an estimator that uses num_particles many samples/particles.
        """
        elbo = 0.0
        for weight, model_trace, guide_trace, log_r in self._get_traces(model, guide, *args, **kwargs):
            elbo_particle = weight * 0

            log_pdf = "batch_log_pdf" if (self.enum_discrete and weight.size(0) > 1) else "log_pdf"
            for name in model_trace.nodes.keys():
                if model_trace.nodes[name]["type"] == "sample":
                    if model_trace.nodes[name]["is_observed"]:
                        elbo_particle += model_trace.nodes[name][log_pdf]
                    else:
                        elbo_particle += model_trace.nodes[name][log_pdf]
                        elbo_particle -= guide_trace.nodes[name][log_pdf]

            # drop terms of weight zero to avoid nans
            if isinstance(weight, numbers.Number):
                if weight == 0.0:
                    elbo_particle = torch_zeros_like(elbo_particle)
            else:
                elbo_particle[weight == 0] = 0.0

            elbo += torch_data_sum(weight * elbo_particle)

        loss = -elbo
        if np.isnan(loss):
            warnings.warn('Encountered NAN loss')
        return loss

    def loss_and_grads(self, model, guide, *args, **kwargs):
        """
        :returns: returns an estimate of the ELBO
        :rtype: float

        ELBO = p(x|z) - KL_qp
        computes teh ELBO by scoring the (collapsed) samples
        drawn from a locally reparameterized distribution
        and the KL analytically from the non collapsed
        """
        elbo = 0.0
        # grab a trace from the generator
        # need to overwrite teh traces in model in guide. probably need a new poutine
        # to do this. it needs to either store the transformed samples
        # sampled from the new dist or calculate a transformed ELBO
        # wshich is the elbo of the original q not the collapsed q'
        for weight, model_trace, guide_trace, log_r in self._get_traces(model, guide, *args, **kwargs):
            elbo_particle = weight * 0
            surrogate_elbo_particle = weight * 0
            # compute elbo and surrogate elbo
            log_pdf = "batch_log_pdf" if (self.enum_discrete and weight.size(0) > 1) else "log_pdf"
            for name, model_site in model_trace.nodes.items():
                if model_site["type"] == "sample":
                    if model_site["is_observed"]:
                        # just need the model likelihood
                        elbo_particle += model_site[log_pdf]
                        surrogate_elbo_particle += model_site[log_pdf]
                    # else:
                    #     guide_site = guide_trace.nodes[name]
                    #     lp = model_site[log_pdf]
                    #     elbo_particle += lp
                    #     if guide_site["fn"].reparameterized:
                    #         surrogate_elbo_particle += lp
                    #     else:
                            # raise Exception('gaussians should be reparameterizable. are your dists correct?')
                            # # XXX should the user be able to control inclusion of the -logq term below?
                            # guide_log_pdf = guide_site[log_pdf] / guide_site["scale"]  # not scaled by subsampling
                            # #log deriv trick
                            # surrogate_elbo_particle += model_site[log_pdf] + log_r.detach() * guide_log_pdf

            log_sigma_p, log_sigma_q, mu_q, mu_p = 0, 0, 0, 0
            for name, model_site in model_trace.nodes.items():
                if 'sigma' in name:
                    guide_site = guide_trace.nodes[name]
                    log_sigma_p += model_site[log_pdf]
                    log_sigma_q += guide_site[log_pdf]
                if 'w0' in name:
                    guide_site = guide_trace.nodes[name]
                    mu_q += guide_site[log_pdf]
                    mu_p += model_site[log_pdf]

            # KL(q||p) given weight samples
            kl_div = (log_sigma_p - log_sigma_q +
                      (torch.exp(2 * log_sigma_q) +
                       torch.pow(torch.exp(mu_q) -
                                 torch.exp(mu_p), 2)) /
                      (2 * torch.exp(2 * log_sigma_p))
                      - 0.5)
            bb()
            # drop terms of weight zero to avoid nans
            if isinstance(weight, numbers.Number):
                if weight == 0.0:
                    elbo_particle = torch_zeros_like(elbo_particle)
                    surrogate_elbo_particle = torch_zeros_like(surrogate_elbo_particle)
            else:
                weight_eq_zero = (weight == 0)
                elbo_particle[weight_eq_zero] = 0.0
                surrogate_elbo_particle[weight_eq_zero] = 0.0

            elbo += torch_data_sum(weight * elbo_particle) - kl_div
            surrogate_elbo_particle = torch_sum(weight * surrogate_elbo_particle) - kl_div

            # collect parameters to train from model and guide
            trainable_params = set(site["value"]
                                   for trace in (model_trace, guide_trace)
                                   for site in trace.nodes.values()
                                   if site["type"] == "param")

            if trainable_params:
                surrogate_loss_particle = -surrogate_elbo_particle
                torch_backward(surrogate_loss_particle)
                pyro.get_param_store().mark_params_active(trainable_params)

        loss = -elbo
        if np.isnan(loss):
            warnings.warn('Encountered NAN loss')
        return loss