from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np

from ray.rllib.models import ModelCatalog


class ProximalPolicyLoss(object):

    other_output = ["vf_preds", "logprobs"]
    is_recurrent = False

    def __init__(
            self, observation_space, action_space,
            observations, value_targets, advantages, actions,
            prev_logits, prev_vf_preds, logit_dim,
            kl_coeff, distribution_class, config, sess, registry):
        self.prev_dist = distribution_class(prev_logits)

        # Saved so that we can compute actions given different observations
        self.observations = observations

        self.curr_logits = ModelCatalog.get_model(
            registry, observations, logit_dim, config["model"]).outputs
        self.curr_dist = distribution_class(self.curr_logits)
        self.sampler = self.curr_dist.sample()

        if config["use_gae"]:
            vf_config = config["model"].copy()
            # Do not split the last layer of the value function into
            # mean parameters and standard deviation parameters and
            # do not make the standard deviations free variables.
            vf_config["free_log_std"] = False
            with tf.variable_scope("value_function"):
                self.value_function = ModelCatalog.get_model(
                    registry, observations, 1, vf_config).outputs
            self.value_function = tf.reshape(self.value_function, [-1])

        # Make loss functions.

        curr_logp = self.curr_dist.logp(actions)
        prev_logp = self.prev_dist.logp(actions)

        self.kl = self.prev_dist.kl(self.curr_dist)
        self.entropy = self.curr_dist.entropy()

        # Lists allow us to handle multiagents and single agents similarly
        if not isinstance(curr_logp, list):
            curr_logp = [curr_logp]
            prev_logp = [self.prev_dist.logp(actions)]
            self.kl = [self.prev_dist.kl(self.curr_dist)]
            self.entropy = [self.curr_dist.entropy()]

        self.ratio = np.asarray([tf.exp(a - b) for a, b
                                 in zip(curr_logp, prev_logp)])
        self.mean_kl = [tf.reduce_mean(mean_kl_i) for mean_kl_i in self.kl]
        self.mean_entropy = [tf.reduce_mean(entropy_i)
                             for entropy_i in self.entropy]
        self.surr1 = [ratio_i * advantages for ratio_i in self.ratio]
        self.surr2 = [advantages *
                      tf.clip_by_value(ratio_i,
                                       1 - config["clip_param"],
                                       1 + config["clip_param"])
                      for ratio_i in self.ratio]
        self.surr = [tf.minimum(surr1_i, surr2_i) for surr1_i, surr2_i
                     in zip(self.surr1, self.surr2)]
        self.mean_policy_loss = tf.reduce_mean(-tf.add_n(self.surr))
        self.surr = tf.add_n(self.surr)
        self.entropy = tf.add_n(self.entropy)
        kl_prod = tf.add_n([kl_coeff[i] * prev_kl for
                            i, prev_kl in enumerate(self.kl)])

        if config["use_gae"]:
            # We use a huber loss here to be more robust against outliers,
            # which seem to occur when the rollouts get longer (the variance
            # scales superlinearly with the length of the rollout)
            self.vf_loss1 = tf.square(self.value_function - value_targets)
            vf_clipped = prev_vf_preds + tf.clip_by_value(
                self.value_function - prev_vf_preds,
                -config["clip_param"], config["clip_param"])
            self.vf_loss2 = tf.square(vf_clipped - value_targets)
            self.vf_loss = tf.minimum(self.vf_loss1, self.vf_loss2)
            self.mean_vf_loss = tf.reduce_mean(self.vf_loss)
            self.loss = tf.reduce_mean(
                -self.surr + kl_prod +
                config["vf_loss_coeff"] * self.vf_loss -
                config["entropy_coeff"] * self.entropy)
        else:
            self.mean_vf_loss = tf.constant(0.0)
            self.loss = tf.reduce_mean(
                -self.surr + kl_prod -
                config["entropy_coeff"] * self.entropy)

        self.sess = sess

        if config["use_gae"]:
            self.policy_results = [
                self.sampler, self.curr_logits, self.value_function]
        else:
            self.policy_results = [
                self.sampler, self.curr_logits, tf.constant("NA")]

    def compute(self, observation):
        action, logprobs, vf = self.sess.run(
            self.policy_results,
            feed_dict={self.observations: [observation]})
        return action[0], {"vf_preds": vf[0], "logprobs": logprobs[0]}

    def loss(self):
        return self.loss
