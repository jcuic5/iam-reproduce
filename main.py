import copy
import glob
import os
import time
from collections import deque

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from a2c_ppo_acktr import algo, utils
from a2c_ppo_acktr.algo import gail
from a2c_ppo_acktr.arguments import get_args
from a2c_ppo_acktr.envs import make_vec_envs
from a2c_ppo_acktr.IAMModel import IAMPolicy
from a2c_ppo_acktr.storage import RolloutStorage
from evaluation import evaluate

import pickle

"""
    NOTE:
    Default arguments overview:
        algo='a2c', alpha=0.99, clip_param=0.2, cuda=True, 
        cuda_deterministic=False, entropy_coef=0.01, env_name='PongNoFrameskip-v4', 
        eps=1e-05, eval_interval=None, gae_lambda=0.95, gail=False, gail_batch_size=128, 
        gail_epoch=5, gail_experts_dir='./gail_experts', gamma=0.99, log_dir='/tmp/gym/', 
        log_interval=10, lr=0.0007, max_grad_norm=0.5, no_cuda=False, num_env_steps=10000000.0, 
        num_mini_batch=32, num_processes=16, num_steps=5, ppo_epoch=4, recurrent_policy=False, 
        save_dir='./trained_models/', save_interval=100, seed=1, use_gae=False, 
        use_linear_lr_decay=False, use_proper_time_limits=False, value_loss_coef=0.5

    So default: 
        num_env_steps 10,000,000 = 125,000 episodes x 5 steps x 16 processes simultaneously
    For the paper we specify the num_env_steps as 4,000,000 and num-steps as 10
"""
def main():
    args = get_args()

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    log_dir = os.path.expanduser(args.log_dir)
    eval_log_dir = log_dir + "_eval"
    utils.cleanup_log_dir(log_dir)
    utils.cleanup_log_dir(eval_log_dir)

    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    envs = make_vec_envs(args.env_name, args.seed, args.num_processes,
                         args.gamma, args.log_dir, device, False)
  
    actor_critic = IAMPolicy(
        envs.observation_space.shape,
        envs.action_space,
        args.env_name,
        base_kwargs={'recurrent': args.recurrent_policy,
                    'IAM': args.IAM})
    actor_critic.to(device)

    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(
            actor_critic,
            args.value_loss_coef,
            args.entropy_coef,
            lr=args.lr,
            eps=args.eps,
            alpha=args.alpha,
            max_grad_norm=args.max_grad_norm)
    elif args.algo == 'ppo':
        agent = algo.PPO(
            actor_critic,
            args.clip_param,
            args.ppo_epoch,
            args.num_mini_batch,
            args.value_loss_coef,
            args.entropy_coef,
            lr=args.lr,
            eps=args.eps,
            max_grad_norm=args.max_grad_norm)
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(
            actor_critic, args.value_loss_coef, args.entropy_coef, acktr=True)

    if args.gail:
        assert len(envs.observation_space.shape) == 1
        discr = gail.Discriminator(
            envs.observation_space.shape[0] + envs.action_space.shape[0], 100,
            device)
        file_name = os.path.join(
            args.gail_experts_dir, "trajs_{}.pt".format(
                args.env_name.split('-')[0].lower()))
        
        expert_dataset = gail.ExpertDataset(
            file_name, num_trajectories=4, subsample_frequency=20)
        drop_last = len(expert_dataset) > args.gail_batch_size
        gail_train_loader = torch.utils.data.DataLoader(
            dataset=expert_dataset,
            batch_size=args.gail_batch_size,
            shuffle=True,
            drop_last=drop_last)

    rollouts = RolloutStorage(args.num_steps, args.num_processes,
                              envs.observation_space.shape, envs.action_space,
                              actor_critic.recurrent_hidden_state_size)

    obs = envs.reset()
    rollouts.obs[0].copy_(obs)
    rollouts.to(device)

    episode_rewards = deque(maxlen=10)
    # ADDED: 
    # Store the mean reward value over processes with the frequency of log_mean_interval
    mean_episode_rewards = []
    max_episode_rewards = []
    log_mean_interval = 10

    start = time.time()
    num_updates = int(
        args.num_env_steps) // args.num_steps // args.num_processes

    for j in range(num_updates):
        # envs.render()
        if args.use_linear_lr_decay:
            # decrease learning rate linearly
            utils.update_linear_schedule(
                agent.optimizer, j, num_updates,
                agent.optimizer.lr if args.algo == "acktr" else args.lr)

        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value, action, action_log_prob, recurrent_hidden_states = actor_critic.act(
                    rollouts.obs[step], rollouts.recurrent_hidden_states[step],
                    rollouts.masks[step])

            # Obser reward and next obs
            obs, reward, done, infos = envs.step(action)

            # ADDED
            if args.flicker:
                prob_flicker = np.random.uniform(0, 1, (obs.shape[0],))
                obs[prob_flicker > 0.5] = 0
            # END ADDED

            for info in infos:
                if 'episode' in info.keys():
                    episode_rewards.append(info['episode']['r'])

            # If done then clean the history of observations.
            masks = torch.FloatTensor(
                [[0.0] if done_ else [1.0] for done_ in done])
            bad_masks = torch.FloatTensor(
                [[0.0] if 'bad_transition' in info.keys() else [1.0]
                 for info in infos])
            rollouts.insert(obs, recurrent_hidden_states, action,
                            action_log_prob, value, reward, masks, bad_masks)

        with torch.no_grad():
            next_value = actor_critic.get_value(
                rollouts.obs[-1], rollouts.recurrent_hidden_states[-1],
                rollouts.masks[-1]).detach()

        if args.gail:
            if j >= 10:
                envs.venv.eval()

            gail_epoch = args.gail_epoch
            if j < 10:
                gail_epoch = 100  # Warm up
            for _ in range(gail_epoch):
                discr.update(gail_train_loader, rollouts,
                             utils.get_vec_normalize(envs)._obfilt)

            for step in range(args.num_steps):
                rollouts.rewards[step] = discr.predict_reward(
                    rollouts.obs[step], rollouts.actions[step], args.gamma,
                    rollouts.masks[step])

        rollouts.compute_returns(next_value, args.use_gae, args.gamma,
                                 args.gae_lambda, args.use_proper_time_limits)

        value_loss, action_loss, dist_entropy = agent.update(rollouts)

        rollouts.after_update()

        # save for every interval-th episode or for the last epoch
        if (j % args.save_interval == 0
                or j == num_updates - 1) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            torch.save([
                actor_critic,
                getattr(utils.get_vec_normalize(envs), 'obs_rms', None)
            ], os.path.join(save_path, args.env_name + ".pt"))

        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            end = time.time()
            print(
                "Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n"
                .format(j, total_num_steps,
                        int(total_num_steps / (end - start)),
                        len(episode_rewards), np.mean(episode_rewards),
                        np.median(episode_rewards), np.min(episode_rewards),
                        np.max(episode_rewards), dist_entropy, value_loss,
                        action_loss))
            
        if (args.eval_interval is not None and len(episode_rewards) > 1
                and j % args.eval_interval == 0):
            obs_rms = utils.get_vec_normalize(envs).obs_rms
            evaluate(actor_critic, obs_rms, args.env_name, args.seed,
                     args.num_processes, eval_log_dir, device)
        
        # ADDED:
        if j % log_mean_interval == 0 and len(episode_rewards) > 1:
            mean_episode_rewards.append(np.mean(episode_rewards))
            max_episode_rewards.append(np.amax(episode_rewards))
    
    # ADDED:
    if args.IAM:
        log_mean_file = log_dir + 'mean_rewards_IAM.txt'
        with open(log_mean_file, 'wb') as f:
            pickle.dump(mean_episode_rewards, f)
    elif args.recurrent_policy:
        log_mean_file = log_dir + 'mean_rewards_GRU.txt'
        with open(log_mean_file, 'wb') as f:
            pickle.dump(mean_episode_rewards, f)
    elif args.num_steps == 10:
        log_mean_file = log_dir + 'mean_rewards_FNN1.txt'
        with open(log_mean_file, 'wb') as f:
            pickle.dump(mean_episode_rewards, f)
    else:
        log_mean_file = log_dir + 'mean_rewards_FNN8.txt'
        with open(log_mean_file, 'wb') as f:
            pickle.dump(mean_episode_rewards, f)


if __name__ == "__main__":
    main()
