#!/usr/bin/env python
"""
The main training script, and testing (if using a non-custom task).

A note about random seeds: the seed we set before drawing the episode's
starting state will determine the initial configuration of objects, for both
standard ravens and any custom envs (so far). Currently, if we ask to set 100
demos, the code will run through seeds 0 through 99. Then, for _test_ demos,
we offset by 10**max_order, so that with 100 demos (max_order=2) we'd start
with 10**2=100 and proceed from there. This way, if doing 20 test episodes,
then ALL snapshots are evaluated on seeds {100, 101, ..., 119}. If we change
max_order=3, which we should for an actual paper, then this simply means the
training is done on {0, 1, ..., 999} and testing starts at seed 1000.

With the custom deformable tasks, I have a separate load.py script. That one
also had max_order=3 (so when doing 100 demos, I was actually starting at
seed 1000, no big deal). However, I now have max_order=4 to start at 10K,
because (a) we will want to use 1000 demos eventually, and (b) for the
deformable tasks, sometimes the initial state might already be 'done', so I
ignore that data and re-sample the starting state with the next seed. Having
load.py start at seed 10K will give us a 'buffer zone' of seeds to protect
the train and test from overlapping.

With goal-conditioning, IF the goals are drawn by sampling from a similar
same starting state distribution (as in the case with insertion-goal) then
use generate_goals.py and set max_order=5 so that there's virtually no chance
of random seed overlap.

When training on a new machine, we can run this script with "1000 demos" for
deformable tasks. It will generate data (but not necessarily "1000 demos"
because max_order determines the actual amount), but exit before we can do
any training, and then we can use subsequent scripts with {1, 10, 100} demos
for actual training.
"""
import datetime
import os
import time
import argparse
import sys
import cv2
import pickle
import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt
from ravens import Dataset, Environment, agents, tasks

from diffusion_policy.network import ConditionalUnet1D
from diffusion_policy.vision_encoder import *
from diffusion_policy.dataset import *
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
from diffusers.training_utils import EMAModel
from diffusers.optimization import get_scheduler
import torch

# Of critical importance! Do 2 for max of 100 demos, 3 for max of 1000 demos.
MAX_ORDER = 3

def diffusion_inference(obs, stats, obs_horizon=1, action_horizon=1):
    """
    Make a 1-step prediction of action based on the observation using a model trained 
    by Diffusion Policy.
    obs: list of 480*640*3
    stats: dict, statistical summary of actions in the dataset.
    Return:
        pose0: tuple, pose of picking 
        pose1: tuple, pose of placing 
    """
    # device transfer
    cond_image = torch.Tensor(obs)
    cond_image = cond_image.permute(2, 0, 1).unsqueeze(0) # [1, 3, 480, 640]
    # cond_image = f(obs)
    nimages = cond_image.to(device, dtype=torch.float32)

    # infer action
    with torch.no_grad():
        # get image features
        image_features = ema_nets['vision_encoder'](nimages)

        # concat with low-dim observations
        obs_features = image_features

        # reshape observation to (B,obs_horizon*obs_dim)
        obs_cond = obs_features.unsqueeze(0).flatten(start_dim=1)

        # initialize action from Guassian noise
        pred_horizon = 1
        action_dim = 4
        noisy_action = torch.randn(
            (1, pred_horizon, action_dim), device=device)
        naction = noisy_action

        # init scheduler
        noise_scheduler.set_timesteps(num_diffusion_iters)

        for k in noise_scheduler.timesteps:
            # predict noise
            noise_pred = ema_nets['noise_pred_net'](
                sample=naction,
                timestep=k,
                global_cond=obs_cond
            )

            # inverse diffusion step (remove noise)
            naction = noise_scheduler.step(
                model_output=noise_pred,
                timestep=k,
                sample=naction
            ).prev_sample

    # unnormalize action
    naction = naction.detach().to('cpu').numpy()
    # (B, pred_horizon, action_dim)
    naction = naction[0]
    action_pred = unnormalize_data(naction, stats=stats['action'])

    # only take action_horizon number of actions
    start = obs_horizon - 1
    end = start + action_horizon
    action = action_pred[start:end,:].flatten() # numpy, (4,)
    ori = (0.0, 0.0, 0.0, 1.0) # quaternion of the grasp
    z_val = 0.001 # for cable-ring
    z_val = 0.14 # for cloth-cover, TODO
    pose0 = (np.append(action[:2], z_val), ori) # pick
    pose1 = (np.append(action[2:], z_val), ori) # place

    return pose0, pose1
    
def rollout(agent, env, stats, args):
    """Standard gym environment rollout. A few clarifications:

    (1) Originally, we did not append the LAST observation and info, since it
    wasn't needed. However I need the last `info` because I record stuff for
    custom envs (e.g., coverage). Don't make it conditioned on 'done' because
    we need it even if done=False, in which case termination is wrt max steps.

    (2) We do a slight hack to save the last observation and last info, which
    we need for goal-conditioning Transporters, and also inspection of data
    to check what the final images look like after the last action.

    (3) Unlike standard gym, the `obs = env.reset(task)` line returns obs={}, an
    EMPTY dict (hence len(obs)==0). Therefore, at t=0, the `episode` list is empty.

    (4) An 'action' normally has these keys: ['camera_config', 'primitive', 'params']
    However, there's a distinction between the ORACLE policies and LEARNED policies.

    (4a) For the ORACLE at t=0, act['primitive'] = None and there is no 'params' key.
    This means that the `env.step(act)` line will NOT actually take the action!
    However, that line WILL return an image observation. So, at t=0, we (a) take no
    action, (b) do not add to `episode` list, (c) but get image observation + info,
    which we can use for the NEXT time step. Thus t=1 is when the first action takes
    place, so for insertion, since it's just one action, we exit the loop when t=1,
    and `len(episode) = 1`. The same applies for learned Transporter policies.

    (4b) For the learned gt_state policies, at t=0, there WILL be a 'params' key, and
    act['primitive'] is not None, hence at t=0 the first action actually takes place.
    This won't affect data collection, as we don't use gt_state for data collection,
    hence all the `episode` stuff gets ignored. However, it will be a bit confusing
    because with insertion (for example) episodes can succeed in one action, but the
    code says they are 'length 0.'

    The reason for (4a) & (4b) can be seen in how these agents implement their `act`
    function. The oracle and Transporter-based policies will return 'empty' actions
    if obs={}, because they rely on images. (Well, some tasks mean the oracle doesn't
    neeed images, but for consistency, the oracle follows the same interface.)

    Anyway, keep this in mind. Actually, the practical effect might be that any
    gt_state policies should run for task.max_steps MINUS one, right? The easiest
    way might be to make the rollout start at t=0 for these agents.

    Returns:
        total_reward: scalar reward signal from the episode, usually 1.0 for
            demonstrators of standard ravens tasks.
        episode: a list of (obs,act,info) tuples used to add to the dataset,
            which formats it for sampling later in training.
        t: time steps (i.e., actions takens) for this episode. If t=0 then
            something bad happened and we shouldn't count this episode.
        last_obs_info: tuple of the (obs,info) at the final time step, the
            one that doesn't have an action.
    """
    start_t = 0
    if args.agent in ['gt_state', 'gt_state_2_step']:
        start_t = 1
    episode = []
    total_reward = 0
    obs = env.reset(task)
    info = env.info
    for t in range(start_t, task.max_steps):
        # print("!!!!!obs ", len(obs['color'][0])) if len(list(obs.keys()))>=2 else None # ['color', 'depth'], obs['color'][0]: list of 480*640*3
        act = agent.act(obs, info)
        if len(list(obs.keys()))>=2: # take the act only in the first empty loop
            obs_rgb = obs['color'][0]
            pose0, pose1 = diffusion_inference(obs_rgb, stats)
            act['params']['pose0'] = pose0
            act['params']['pose1'] = pose1

        if len(obs) > 0 and act['primitive']:
            episode.append((obs, act, info))
        (obs, reward, done, info) = env.step(act)
        total_reward += reward
        last_obs_info = (obs, info)
        if done:
            break
    return total_reward, episode, t, last_obs_info


def has_deformables(task):
    """
    Somewhat misleading name. This method is used to determine if we should
    (a) be running training AFTER environment data collection, and (b)
    evaluating with test-time rollouts periodically. For (a) the reason was
    the --disp option, which is needed to see cloth, will not let us run
    multiple Environment calls. This also applies to (b), but for (b) there's
    an extra aspect: most of these new environments have reward functions
    that are not easily interpretable, or they have extra stuff in `info`
    that would be better for us to use. In that case we should be using
    `load.py` to roll out these policies.

    Actually, even for stuff like cable-shape, where the reward really tells
    us all we need to know, it would be nice to understand failure cases
    based on the type of the target. So for now let's set it to record
    `cable-` which will ignore `cable` but catch all my custom environments.
    The custom cable environments don't use --disp but let's just have them
    here for consistency in the main-then-load paradigm.
    """
    return ('cable-' in task) or ('cloth' in task) or ('bag' in task)


def is_goal_conditioned(args):
    """
    Be careful with checking this condition. See `generate_goals.py`. Here,
    though, we check the task name and as an extra safety measure, check that
    the agent is also named with 'goal'.

    Update: all right, let's modify this to incorpoate gt_state w/out too much
    extra work. :(
    """
    goal_tasks = ['insertion-goal', 'cable-shape-notarget', 'cable-line-notarget',
            'cloth-flat-notarget', 'bag-color-goal']
    goal_task = (args.task in goal_tasks)
    if goal_task:
        assert 'goal' in args.agent or 'gt_state' in args.agent, \
            'Agent should be a goal-based agent, or gt_state agent.'
    return goal_task


def ignore_this_demo(args, demo_reward, t, last_extras):
    """In some cases, we should filter out demonstrations.

    Filter for if t == 0, which means the initial state was a success.
    Also, for the bag envs, if we end up in a catastrophic state, I exit
    gracefully and we should avoid those demos (they won't have images we
    need for the dataset anyway).
    """
    ignore = (t == 0)

    # For bag envs.
    if 'exit_gracefully' in last_extras:
        assert last_extras['exit_gracefully']
        return True

    # Another bag env.
    if (args.task in ['bag-color-goal']) and demo_reward <= 0.5:
        return True

    # Another bag env. We can get 0.5 reward by touching the cube only (bad).
    if args.task == 'bag-items-easy' and demo_reward <= 0.5:
        return True

    # Harder bags: ignore if (a) no beads in zone, OR, (b) didn't get both
    # items. Need separate case because we could get all beads (0.5) but only
    # one item (0.25) which sums to 0.75. However, we can also get both items
    # (0.5) and a few beads (e.g., 0.1) which sums to 0.6.
    if args.task == 'bag-items-hard':
        return (last_extras['zone_items_rew'] < 0.5 or
                last_extras['zone_beads_rew'] == 0)

    return ignore

def get_network():
    # **Network Demo**
    obs_horizon = 1

    # construct ResNet18 encoder
    # if you have multiple camera views, use seperate encoder weights for each view.
    vision_encoder = get_resnet('resnet18')

    # IMPORTANT!
    # replace all BatchNorm with GroupNorm to work with EMA
    # performance will tank if you forget to do this!
    vision_encoder = replace_bn_with_gn(vision_encoder)

    # ResNet18 has output dim of 512
    vision_feature_dim = 512
    # agent_pos is 2 dimensional
    # lowdim_obs_dim =2
    lowdim_obs_dim = 0
    # observation feature has 514 dims in total per step
    obs_dim = vision_feature_dim + lowdim_obs_dim
    action_dim = 4

    # create network object
    noise_pred_net = ConditionalUnet1D(
        input_dim=action_dim,
        global_cond_dim=obs_dim*obs_horizon
    )

    # the final arch has 2 parts
    nets = nn.ModuleDict({
        'vision_encoder': vision_encoder,
        'noise_pred_net': noise_pred_net
    })

    return nets

if __name__ == '__main__':
    # Parse command line arguments.
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu',            default='0')
    parser.add_argument('--disp',           action='store_true')
    parser.add_argument('--task',           default='hanoi')
    parser.add_argument('--agent',          default='transporter')
    parser.add_argument('--num_demos',      default='100')
    parser.add_argument('--num_rots',       default=24, type=int)
    parser.add_argument('--hz',             default=240.0, type=float)
    parser.add_argument('--gpu_mem_limit',  default=None)
    parser.add_argument('--subsamp_g',      action='store_true')
    parser.add_argument('--crop_bef_q',     default=0, type=int, help='CoRL paper used 1')
    parser.add_argument('--save_zero',      action='store_true', help='Save snapshot at 0 iterations')
    args = parser.parse_args()

    # Configure which GPU to use.
    cfg = tf.config.experimental
    gpus = cfg.list_physical_devices('GPU')
    if len(gpus) == 0:
        print('No GPUs detected. Running with CPU.')
    else:
        cfg.set_visible_devices(gpus[int(args.gpu)], 'GPU')

    # Configure how much GPU to use.
    if args.gpu_mem_limit is not None:
        MEM_LIMIT = 1024 * int(args.gpu_mem_limit)
        print(args.gpu_mem_limit)
        dev_cfg = [cfg.VirtualDeviceConfiguration(memory_limit=MEM_LIMIT)]
        cfg.set_virtual_device_configuration(gpus[0], dev_cfg)

    # Initialize task. Later, initialize Environment if necessary.
    task = tasks.names[args.task]()
    dataset = Dataset(os.path.join('data', args.task))
    if args.subsamp_g:
        dataset.subsample_goals = True

    # Loading Pretrained Checkpoint
    # Set `load_pretrained = True` to load pretrained weights.
    load_pretrained = 1
    if load_pretrained:
        # ckpt_path = "/home/yif/Documents/KTH/git/DeformableDiffusion/sf24.ckpt"
        ckpt_path = "/home/yif/Documents/KTH/git/DeformableDiffusion/rb28.ckpt" # TODO
        if not os.path.isfile(ckpt_path):
            print('Path not found!')

        state_dict = torch.load(ckpt_path, map_location='cuda')
        nets = get_network()
        ema_nets = nets
        ema_nets.load_state_dict(state_dict["model_state_dict"])
        print('Pretrained weights loaded.')
    else:
        print("Skipped pretrained weight loading.")

    # Evaluate on increasing orders of magnitude of demonstrations.
    num_train_runs = 3  # to measure variance over random initialization
    num_train_iters = 20000
    test_interval = 2000
    if args.save_zero:
        num_train_runs = 1      # let's keep it simple
        test_interval = 0       # this is what gets passed to agent.train()
    num_test_episodes = 20
    if not os.path.exists('test_results'):
        os.makedirs('test_results')

    # Check if it's goal-conditioned.
    goal_conditioned = is_goal_conditioned(args)

    # for this demo, we use DDPMScheduler with 100 diffusion iterations
    num_diffusion_iters = 100
    noise_scheduler = DDPMScheduler(
        num_train_timesteps=num_diffusion_iters,
        # the choise of beta schedule has big impact on performance
        # we found squared cosine works the best
        beta_schedule='squaredcos_cap_v2',
        # clip output to [-1,1] to improve stability
        clip_sample=True,
        # our network predicts noise (instead of denoised action)
        prediction_type='epsilon'
    )

    # device transfer
    device = torch.device('cuda')
    _ = nets.to(device)

    # Dataset statistic
    # stats = {'action': {'min': np.array([ 0.2812497 , -0.46875015,  0.2575122 , -0.48762894], dtype=np.float32), 
    #                 'max': np.array([0.71875143, 0.46875057, 0.74267364, 0.47370854], dtype=np.float32)}} # for cloth-cover, TODO
    stats = {'action': {'min': np.array([ 0.1321154 , -0.5868091 ,  0.3375    , -0.40937498], dtype=np.float32),
                    'max': np.array([0.90098524, 0.5565594 , 0.66249996, 0.4110589 ], dtype=np.float32)}} # for cable-ring

    # Do multiple training runs from scratch with TensorFlow random initialization.
    for train_run in range(num_train_runs):
        # Set up tensorboard logger.
        current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        train_log_dir = os.path.join('logs', args.agent, args.task, current_time, 'train')
        train_summary_writer = tf.summary.create_file_writer(train_log_dir)

        # Set the beginning of the agent name.
        name = f'{args.task}-{args.agent}-{args.num_demos}-{train_run}'

        # Initialize agent and limit random dataset sampling to fixed set.
        tf.random.set_seed(train_run)
        if args.agent == 'transporter':
            name = f'{name}-rots-{args.num_rots}-crop_bef_q-{args.crop_bef_q}'
            agent = agents.names[args.agent](name,
                                             args.task,
                                             num_rotations=args.num_rots,
                                             crop_bef_q=(args.crop_bef_q == 1))
        elif 'transporter-goal' in args.agent:
            assert goal_conditioned
            name = f'{name}-rots-{args.num_rots}'
            if args.subsamp_g:
                name += '-sub_g'
            else:
                name += '-fin_g'
            agent = agents.names[args.agent](name,
                                             args.task,
                                             num_rotations=args.num_rots)
        elif 'gt_state' in args.agent:
            agent = agents.names[args.agent](name,
                                             args.task,
                                             goal_conditioned=goal_conditioned)
        else:
            agent = agents.names[args.agent](name, args.task)

        performance = []
        while agent.total_iter < num_train_iters:
            # YD: test set
            # task.mode = 'test'
            env = Environment(args.disp, hz=args.hz)
            for episode in range(num_test_episodes):
                seed = 10**MAX_ORDER + episode
                np.random.seed(seed)
                total_reward, epi, t, _ = rollout(agent, env, stats, args)
                # print("episode ", epi[1][1]) # default pick and place position - [0.5 , 0.  , 0.14]
                print(f'Test (seed: {seed}): {episode} Total Reward: {total_reward:.2f}, len: {t}')
                performance.append((agent.total_iter, total_reward))
                print(" ")
            env.stop()
            del env

            # Save results.
            fname = os.path.join('test_results', f'{name}.pkl')
            pickle.dump(performance, open(fname, 'wb'))
