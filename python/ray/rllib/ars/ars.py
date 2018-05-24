'''
Parallel implementation of the Augmented Random Search method.
Horia Mania --- hmania@berkeley.edu
Aurelia Guy
Benjamin Recht 
'''

import parser
import time
import os
import pickle
import numpy as np
import gym
import ray
from ray.rllib.es import utils
from ray.rllib.ars import optimizers
from ray.rllib import agent
from collections import namedtuple
from ray.rllib.ars.policies import *
from ray.rllib.es import tabular_logger as tlogger
import socket
import ray.tune as tune
from ray.tune import grid_search

Result = namedtuple("Result", [
    "noise_indices", "noisy_returns", "sign_noisy_returns", "noisy_lengths",
    "eval_returns", "eval_lengths"
])


DEFAULT_CONFIG = dict(
    policy_params=None,
    num_workers=32,
    num_deltas=320,
    deltas_used=320,
    delta_std=0.02,
    logdir=None,
    rollout_length=1000,
    step_size=0.01,
    shift='constant zero',
    observation_filter='MeanStdFilter',
    params=None,
    seed=123,
    env_config={}
)


@ray.remote
def create_shared_noise():
    """
    Create a large array of noise to be shared by all workers. Used
    for avoiding the communication of the random perturbations delta.
    """

    seed = 12345
    count = 250000000
    noise = np.random.RandomState(seed).randn(count).astype(np.float64)
    return noise


class SharedNoiseTable(object):
    def __init__(self, noise, seed = 11):

        self.rg = np.random.RandomState(seed)
        self.noise = noise
        assert self.noise.dtype == np.float64

    def get(self, i, dim):
        return self.noise[i:i + dim]

    def sample_index(self, dim):
        return self.rg.randint(0, len(self.noise) - dim + 1)

    def get_delta(self, dim):
        idx = self.sample_index(dim)
        return idx, self.get(idx, dim)


@ray.remote
class Worker(object):
    """ 
    Object class for parallel rollout generation.
    """

    def __init__(self, registry, config,  env_creator,
                 env_seed,
                 deltas=None,
                 rollout_length=1000,
                 delta_std=0.02):

        # initialize OpenAI environment for each worker
        self.env = env_creator(config["env_config"])
        self.env.seed(env_seed)

        from ray.rllib import models
        self.preprocessor = models.ModelCatalog.get_preprocessor(
            registry, self.env)

        # each worker gets access to the shared noise table
        # with independent random streams for sampling
        # from the shared noise table. 
        self.deltas = SharedNoiseTable(deltas, env_seed + 7)

        from ray.rllib import models
        self.preprocessor = models.ModelCatalog.get_preprocessor(
            registry, self.env)

        self.delta_std = delta_std
        self.rollout_length = rollout_length
        self.sess = utils.make_session(single_threaded=True)
        self.policy = LinearPolicy(
            registry, self.sess, self.env.action_space, self.preprocessor,
            config["observation_filter"])

        
    def get_weights_plus_stats(self):
        """ 
        Get current policy weights and current statistics of past states.
        """
        return self.policy.get_weights_plus_stats()
    

    def rollout(self, shift = 0., rollout_length = None):
        """ 
        Performs one rollout of maximum length rollout_length. 
        At each time-step it substracts shift from the reward.
        """
        
        if rollout_length is None:
            rollout_length = self.rollout_length

        total_reward = 0.
        steps = 0

        ob = self.env.reset()
        for i in range(rollout_length):
            action = self.policy.compute(ob)
            ob, reward, done, _ = self.env.step(action)
            steps += 1
            total_reward += (reward - shift)
            if done:
                break
            
        return total_reward, steps

    def do_rollouts(self, w_policy, num_rollouts = 1, shift = 1, evaluate = False):
        """ 
        Generate multiple rollouts with a policy parametrized by w_policy.
        """

        rollout_rewards, deltas_idx = [], []
        steps = 0

        for i in range(num_rollouts):

            if evaluate:
                self.policy.set_weights(w_policy)
                deltas_idx.append(-1)
                
                # set to false so that evaluation rollouts are not used for updating state statistics
                self.policy.update_filter = False

                # for evaluation we do not shift the rewards (shift = 0) and we use the
                # default rollout length (1000 for the MuJoCo locomotion tasks)
                reward, r_steps = self.rollout(shift = 0., rollout_length = self.env.spec.timestep_limit)
                rollout_rewards.append(reward)
                
            else:
                idx, delta = self.deltas.get_delta(w_policy.size)
             
                delta = (self.delta_std * delta).reshape(w_policy.shape)
                deltas_idx.append(idx)

                # set to true so that state statistics are updated 
                self.policy.update_filter = True

                # compute reward and number of timesteps used for positive perturbation rollout
                self.policy.set_weights(w_policy + delta)
                pos_reward, pos_steps  = self.rollout(shift = shift)

                # compute reward and number of timesteps used for negative pertubation rollout
                self.policy.set_weights(w_policy - delta)
                neg_reward, neg_steps = self.rollout(shift = shift) 
                steps += pos_steps + neg_steps

                rollout_rewards.append([pos_reward, neg_reward])
                            
        return {'deltas_idx': deltas_idx, 'rollout_rewards': rollout_rewards, "steps" : steps}
    
    def stats_increment(self):
        self.policy.observation_filter.stats_increment()
        return

    def get_weights(self):
        return self.policy.get_weights()
    
    def get_filter(self):
        return self.policy.observation_filter

    def sync_filter(self, other):
        self.policy.observation_filter.sync(other)
        return

    
class ARSAgent(agent.Agent):
    """ 
    Object class implementing the ARS algorithm.
    """
    _agent_name = "ARS"
    _default_config = DEFAULT_CONFIG
    _allow_unknown_subkeys = ["env_config"]

    def _init(self):

        env = self.env_creator(self.config["env_config"])
        from ray.rllib import models
        preprocessor = models.ModelCatalog.get_preprocessor(
            self.registry, env)

        self.timesteps = 0
        self.n_iter = 1000
        self.action_size = env.action_space.shape[0]
        self.ob_size = env.observation_space.shape[0]
        self.num_deltas = self.config["num_deltas"]
        self.deltas_used = self.config["deltas_used"]
        self.rollout_length = self.config["rollout_length"]
        self.step_size = self.config["step_size"]
        self.delta_std = self.config["delta_std"]
        seed = self.config["seed"]
        self.logdir = self.config["logdir"]
        self.shift = self.config["shift"]
        self.max_past_avg_reward = float('-inf')
        self.num_episodes_used = float('inf')

        # Create the shared noise table.
        print("Creating shared noise table.")
        noise_id = create_shared_noise.remote()
        self.deltas = SharedNoiseTable(ray.get(noise_id), seed=seed + 3)

        # Create the actors.
        print("Creating actors.")
        self.num_workers = self.config["num_workers"]
        self.workers = [
            Worker.remote(
                self.registry, self.config, self.env_creator,
                seed + 7 * i,
                deltas=noise_id,
                rollout_length=self.rollout_length,
                delta_std=self.delta_std)
            for i in range(self.config["num_workers"])]

        self.sess = utils.make_session(single_threaded=False)
        # initialize policy 
        self.policy = LinearPolicy(
        self.registry, self.sess, env.action_space, preprocessor,
        self.config["observation_filter"])
        self.w_policy = self.policy.get_weights()

            
        # initialize optimization algorithm
        self.optimizer = optimizers.SGD(self.w_policy, self.config["step_size"])
        print("Initialization of ARS complete.")

    def aggregate_rollouts(self, num_rollouts = None, evaluate = False):
        """ 
        Aggregate update step from rollouts generated in parallel.
        """

        if num_rollouts is None:
            num_deltas = self.num_deltas
        else:
            num_deltas = num_rollouts
            
        # put policy weights in the object store
        policy_id = ray.put(self.w_policy)

        t1 = time.time()
        num_rollouts = int(num_deltas / self.num_workers)
            
        # parallel generation of rollouts
        rollout_ids_one = [worker.do_rollouts.remote(policy_id,
                                                 num_rollouts = num_rollouts,
                                                 shift = self.shift,
                                                 evaluate=evaluate) for worker in self.workers]

        rollout_ids_two = [worker.do_rollouts.remote(policy_id,
                                                 num_rollouts = 1,
                                                 shift = self.shift,
                                                 evaluate=evaluate) for worker in self.workers[:(num_deltas % self.num_workers)]]

        # gather results 
        results_one = ray.get(rollout_ids_one)
        results_two = ray.get(rollout_ids_two)

        rollout_rewards, deltas_idx = [], [] 

        for result in results_one:
            if not evaluate:
                self.timesteps += result["steps"]
            deltas_idx += result['deltas_idx']
            rollout_rewards += result['rollout_rewards']

        for result in results_two:
            if not evaluate:
                self.timesteps += result["steps"]
            deltas_idx += result['deltas_idx']
            rollout_rewards += result['rollout_rewards']

        deltas_idx = np.array(deltas_idx)
        rollout_rewards = np.array(rollout_rewards, dtype = np.float64)
        
        print('Maximum reward of collected rollouts:', rollout_rewards.max())
        t2 = time.time()

        print('Time to generate rollouts:', t2 - t1)

        if evaluate:
            return rollout_rewards

        # select top performing directions if deltas_used < num_deltas
        max_rewards = np.max(rollout_rewards, axis = 1)
        if self.deltas_used > self.num_deltas:
            self.deltas_used = self.num_deltas
            
        idx = np.arange(max_rewards.size)[max_rewards >= np.percentile(max_rewards, 100*(1 - (self.deltas_used / self.num_deltas)))]
        deltas_idx = deltas_idx[idx]
        rollout_rewards = rollout_rewards[idx,:]
        
        # normalize rewards by their standard deviation
        rollout_rewards /= np.std(rollout_rewards)

        t1 = time.time()
        # aggregate rollouts to form g_hat, the gradient used to compute SGD step
        g_hat, count = utils.batched_weighted_sum(rollout_rewards[:,0] - rollout_rewards[:,1],
                                                  (self.deltas.get(idx, self.w_policy.size)
                                                   for idx in deltas_idx),
                                                  batch_size = 500)
        g_hat /= deltas_idx.size
        t2 = time.time()
        print('time to aggregate rollouts', t2 - t1)
        return g_hat
        

    def train_step(self):
        """ 
        Perform one update step of the policy weights.
        """
        
        g_hat = self.aggregate_rollouts()                    
        print("Euclidean norm of update step:", np.linalg.norm(g_hat))
        self.w_policy -= self.optimizer._compute_step(g_hat).reshape(self.w_policy.shape)
        return

    def _train(self):

        start = time.time()
        for i in range(self.n_iter):
            
            t1 = time.time()
            self.train_step()
            t2 = time.time()
            print('total time of one step', t2 - t1)           
            print('iter ', i,' done')

            # record statistics every 10 iterations
            if ((i + 1) % 10 == 0):
                
                rewards = self.aggregate_rollouts(num_rollouts = 100, evaluate = True)
                w = ray.get(self.workers[0].get_weights_plus_stats.remote())
                np.savez(self.logdir + "/lin_policy_plus", w)
                
                # print(sorted(self.params.items()))
                # logz.log_tabular("Time", time.time() - start)
                # logz.log_tabular("Iteration", i + 1)
                # logz.log_tabular("AverageReward", np.mean(rewards))
                # logz.log_tabular("StdRewards", np.std(rewards))
                # logz.log_tabular("MaxRewardRollout", np.max(rewards))
                # logz.log_tabular("MinRewardRollout", np.min(rewards))
                # logz.log_tabular("timesteps", self.timesteps)
                # logz.dump_tabular()

                step_tend = time.time()
                tlogger.record_tabular("EvalEpRewMean", np.mean(rewards))
                tlogger.record_tabular("EvalEpRewStd", np.std(rewards))
                # tlogger.record_tabular("EvalEpLenMean", eval_lengths.mean())
                #
                # tlogger.record_tabular("EpRewMean", noisy_returns.mean())
                # tlogger.record_tabular("EpRewStd", noisy_returns.std())
                # tlogger.record_tabular("EpLenMean", noisy_lengths.mean())
                #
                # tlogger.record_tabular("Norm", float(np.square(theta).sum()))
                # tlogger.record_tabular("GradNorm", float(np.square(g).sum()))
                # tlogger.record_tabular("UpdateRatio", float(update_ratio))
                #
                # tlogger.record_tabular("EpisodesThisIter", noisy_lengths.size)
                # tlogger.record_tabular("EpisodesSoFar", self.episodes_so_far)
                # tlogger.record_tabular("TimestepsThisIter", noisy_lengths.sum())
                # tlogger.record_tabular("TimestepsSoFar", self.timesteps_so_far)
                #
                # tlogger.record_tabular("TimeElapsedThisIter", step_tend - step_tstart)
                # tlogger.record_tabular("TimeElapsed", step_tend - self.tstart)
                tlogger.dump_tabular()

                info = {
                    "weights_norm": 0,#np.square(theta).sum(),
                    "grad_norm": 0,#np.square(g).sum(),
                    "update_ratio": 0,#update_ratio,
                    "episodes_this_iter": 0,#noisy_lengths.size,
                    "episodes_so_far": 0,#self.episodes_so_far,
                    "timesteps_this_iter": 0,#noisy_lengths.sum(),
                    "timesteps_so_far": 0,#self.timesteps_so_far,
                    "time_elapsed_this_iter": 0,#step_tend - step_tstart,
                    "time_elapsed": 0,#step_tend - self.tstart
                }

                result = ray.tune.result.TrainingResult(
                    episode_reward_mean=np.mean(rewards),#eval_returns.mean(),
                    episode_len_mean=100,#eval_lengths.mean(),
                    timesteps_this_iter=100,#noisy_lengths.sum(),
                    info=info)

                return result
                
            t1 = time.time()
            # get statistics from all workers
            for j in range(self.num_workers):
                self.policy.observation_filter.update(ray.get(self.workers[j].get_filter.remote()))
            self.policy.observation_filter.stats_increment()

            # make sure master filter buffer is clear
            self.policy.observation_filter.clear_buffer()
            # sync all workers
            filter_id = ray.put(self.policy.observation_filter)
            setting_filters_ids = [worker.sync_filter.remote(filter_id) for worker in self.workers]
            # waiting for sync of all workers
            ray.get(setting_filters_ids)
         
            increment_filters_ids = [worker.stats_increment.remote() for worker in self.workers]
            # waiting for increment of all workers
            ray.get(increment_filters_ids)            
            t2 = time.time()
            print('Time to sync statistics:', t2 - t1)
                        
        return

    def _stop(self):
        # workaround for https://github.com/ray-project/ray/issues/1516
        for w in self.workers:
            w.__ray_terminate__.remote(w._ray_actor_id.id())

    def _save(self, checkpoint_dir):
        checkpoint_path = os.path.join(
            checkpoint_dir, "checkpoint-{}".format(self.iteration))
        weights = self.policy.get_weights()
        objects = [
            weights,
            self.episodes_so_far,
            self.timesteps_so_far]
        pickle.dump(objects, open(checkpoint_path, "wb"))
        return checkpoint_path

    def _restore(self, checkpoint_path):
        objects = pickle.load(open(checkpoint_path, "rb"))
        self.policy.set_weights(objects[0])
        self.episodes_so_far = objects[1]
        self.timesteps_so_far = objects[2]

    def compute_action(self, observation):
        return self.policy.compute(observation, update=False)[0]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--env_name', type=str, default='HalfCheetah-v2')
    parser.add_argument('--n_iter', '-n', type=int, default=[1000])
    parser.add_argument('--n_directions', '-nd', type=int, default=[8])
    parser.add_argument('--deltas_used', '-du', type=int, default=[8])
    parser.add_argument('--step_size', '-s', type=float, default=[0.02])
    parser.add_argument('--delta_std', '-std', type=float, default=[.03])
    parser.add_argument('--n_workers', '-e', type=int, default=[18])
    parser.add_argument('--rollout_length', '-r', type=int, default=[1000])

    # for Swimmer-v1 and HalfCheetah-v1 use shift = 0
    # for Hopper-v1, Walker2d-v1, and Ant-v1 use shift = 1
    # for Humanoid-v1 used shift = 5
    parser.add_argument('--shift', type=float, default=0)
    parser.add_argument('--seed', type=int, default=[237])
    parser.add_argument('--policy_type', type=str, default='linear')
    parser.add_argument('--dir_path', type=str, default='data')

    # for ARS V1 use filter = 'NoFilter'
    parser.add_argument('--filter', type=str, default='MeanStdFilter')

    local_ip = socket.gethostbyname(socket.gethostname())
    ray.init(num_cpus=4, redirect_output=False) #redis_address= local_ip + ':6379')

    args = parser.parse_args()
    params = vars(args)
    #run_ars(params)
    config = DEFAULT_CONFIG
    config["step_size"] = grid_search([.02, .04])
    tune.run_experiments({
        "my_experiment": {
            "run": "ARS",
            "stop": {
                "training_iteration": 200
            },
            "env": 'HalfCheetah-v2',
            # "config": {
            #     "env_name": params["env_name"],
            #     "n_iter": tune.grid_search(params["n_iter"]),
            #     "n_directions": tune.grid_search(params["n_directions"]),
            #     "deltas_used": tune.grid_search(params["deltas_used"]),
            #     "step_size": tune.grid_search(params["step_size"]),
            #     "delta_std": tune.grid_search(params["delta_std"]),
            #     "n_workers": tune.grid_search(params["n_workers"]),
            #     "rollout_length": tune.grid_search(params["rollout_length"]),
            #     "shift": params["shift"],
            #     "seed": tune.grid_search(params["seed"]),
            #     "policy_type": params["policy_type"],
            #     "dir_path": params["dir_path"],
            #     "filter": params["filter"]
            # }
            "config": config,
            "trial_resources": {
                "cpu": 1,
                "gpu": 0,
                "extra_cpu": 3 - 1,
            }
        }
    })