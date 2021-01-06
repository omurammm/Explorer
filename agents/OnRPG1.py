from agents.PPO import *
from torch.distributions import Normal


class OnRPG1(PPO):
  '''
  Implementation of OnRPG (On-policy Reward Policy Gradient)
  '''
  def __init__(self, cfg):
    super().__init__(cfg)
    # Set optimizer for reward function
    self.optimizer['reward'] = getattr(torch.optim, cfg['optimizer']['name'])(self.network.reward_params, **cfg['optimizer']['critic_kwargs'])
    # Set replay buffer
    self.replay = FiniteReplay(self.cfg['steps_per_epoch'], keys=['state', 'action', 'reward', 'next_state', 'mask', 'log_pi', 'ret', 'adv', 'v'])
    if cfg['state_normalizer']:
      self.state_normalizer = MeanStdNormalizer()

  def createNN(self, input_type):
    # Set feature network
    if input_type == 'pixel':
      input_size = self.cfg['feature_dim']
      if 'MinAtar' in self.env_name:
        feature_net = Conv2d_MinAtar(in_channels=self.env[mode].game.state_shape()[2], feature_dim=input_size)
      else:
        feature_net = Conv2d_Atari(in_channels=4, feature_dim=input_size)
    elif input_type == 'feature':
      input_size = self.state_size
      feature_net = nn.Identity()
    # Set actor network
    assert self.action_type == 'CONTINUOUS', f"{self.cfg['agent']['name']} only supports continous action spaces."
    if self.cfg['actor_select'] == 'MLPGaussianActor':
      actor_net = MLPGaussianActor(action_lim=self.action_lim, layer_dims=[input_size]+self.cfg['hidden_layers']+[self.action_size], hidden_act=self.cfg['hidden_act'], rsample=False)
    elif self.cfg['actor_select'] == 'MLPStdGaussianActor':
      actor_net = MLPStdGaussianActor(action_lim=self.action_lim, layer_dims=[input_size]+self.cfg['hidden_layers']+[2*self.action_size], hidden_act=self.cfg['hidden_act'], rsample=False)
    elif self.cfg['actor_select'] == 'MLPSquashedGaussianActor':
      actor_net = MLPSquashedGaussianActor(action_lim=self.action_lim, layer_dims=[input_size]+self.cfg['hidden_layers']+[2*self.action_size], hidden_act=self.cfg['hidden_act'], rsample=False)
    # Set critic network (state value)
    critic_net = MLPCritic(layer_dims=[input_size]+self.cfg['hidden_layers']+[1], hidden_act=self.cfg['hidden_act'], output_act=self.cfg['output_act'])
    # Set reward network
    reward_net = MLPQCritic(layer_dims=[input_size+self.action_size]+self.cfg['hidden_layers']+[1], hidden_act=self.cfg['hidden_act'], output_act=self.cfg['output_act'])
    # Set the model
    NN = ActorVCriticRewardNet(feature_net, actor_net, critic_net, reward_net)
    return NN

  def save_experience(self, prediction):
    # Save state, action, reward, next_state, mask, log_pi
    mode = 'Train'
    prediction = {
      'state': to_tensor(self.state[mode], self.device),
      'action': to_tensor(self.action[mode], self.device),
      'reward': to_tensor(self.reward[mode], self.device),
      'next_state': to_tensor(self.next_state[mode], self.device),
      'mask': to_tensor(1-self.done[mode], self.device),
      'log_pi': prediction['log_pi'].detach(),
      'v': prediction['v'].detach()
    }
    self.replay.add(prediction)

  def learn(self):
    mode = 'Train'
    # Compute return and advantage
    adv = torch.tensor(0.0)
    ret = self.network.get_state_value(self.replay.next_state[-1]).detach()
    self.replay.v.append(ret)
    self.replay.adv[-1] = ret.detach()
    for i in reversed(range(self.cfg['steps_per_epoch'])):
      ret = self.replay.reward[i] + self.discount * self.replay.mask[i] * ret
      self.replay.ret[i] = ret.detach()
      if self.cfg['gae'] < 0:
        adv = ret - self.replay.v[i].detach()
      else:
        td_error = self.replay.reward[i] + self.discount * self.replay.mask[i] * self.replay.v[i+1] - self.replay.v[i]
        adv = self.discount * self.cfg['gae'] * self.replay.mask[i] * adv + td_error
      if i >= 1: # use lambda return as the advantage
        self.replay.adv[i-1] = (adv + self.replay.v[i]).detach()
    # Get training data
    entries = self.replay.get(['state', 'action', 'next_state', 'reward', 'mask', 'log_pi', 'ret', 'adv'], self.cfg['steps_per_epoch'])
    # Compute advantage
    # v_next = entries.mask * self.network.get_state_value(entries.next_state).detach()
    if self.show_tb:
      # self.logger.add_scalar('original_adv', v_next.mean().item(), self.step_count)
      self.logger.add_scalar('original_adv', entries.adv.mean().item(), self.step_count)
    if self.cfg['adv'] == 'std':
      entries.adv.copy_((entries.adv - entries.adv.mean()) / entries.adv.std())
    elif self.cfg['adv'] == 'baseline1':
      entries.adv.copy_(entries.adv - entries.adv.mean())
    elif self.cfg['adv'] == 'baseline2':
      entries.adv.copy_(entries.adv - self.network.get_state_value(entries.state).detach())
    elif self.cfg['adv'] == 'vanilla':
      pass
    # Optimize for multiple epochs
    for _ in range(self.cfg['optimize_epochs']):
      batch_idxs = generate_batch_idxs(len(entries.log_pi), self.cfg['batch_size'])
      for batch_idx in batch_idxs:
        batch_idx = to_tensor(batch_idx, self.device).long()
        new_log_pi = self.network.get_log_pi(entries.state[batch_idx], entries.action[batch_idx])
        # Take an optimization step for actor
        approx_kl = (entries.log_pi[batch_idx] - new_log_pi).mean()
        if approx_kl <= 1.5 * self.cfg['target_kl']:
          # Freeze reward network to avoid computing gradients for it
          for p in self.network.reward_net.parameters():
            p.requires_grad = False
          # Get predicted reward
          repara_action = self.network.get_repara_action(entries.state[batch_idx], entries.action[batch_idx])
          predicted_reward = self.network.get_reward(entries.state[batch_idx], repara_action)
          # Compute clipped objective
          obj = predicted_reward + self.discount * entries.adv[batch_idx] * new_log_pi
          ratio = torch.exp(new_log_pi - entries.log_pi[batch_idx]).detach()
          obj_clipped = torch.clamp(ratio, 1-self.cfg['clip_ratio'], 1+self.cfg['clip_ratio']) * obj
          actor_loss = -torch.min(ratio*obj, obj_clipped).mean()
          self.optimizer['actor'].zero_grad()
          actor_loss.backward()
          if self.gradient_clip > 0:
            nn.utils.clip_grad_norm_(self.network.actor_params, self.gradient_clip)
          self.optimizer['actor'].step()
          # Unfreeze reward network
          for p in self.network.reward_net.parameters():
            p.requires_grad = True
        # Take an optimization step for critic
        v = self.network.get_state_value(entries.state[batch_idx])
        if self.cfg['critic_loss'] == 'OneStep':
          critic_loss = (entries.reward[batch_idx] + self.discount * v_next[batch_idx] - v).pow(2).mean()
        elif self.cfg['critic_loss'] == 'ret':
          critic_loss = (entries.ret[batch_idx] - v).pow(2).mean()
        self.optimizer['critic'].zero_grad()
        critic_loss.backward()
        if self.gradient_clip > 0:
          nn.utils.clip_grad_norm_(self.network.critic_params, self.gradient_clip)
        self.optimizer['critic'].step()
        # Take an optimization step for reward
        predicted_reward = self.network.get_reward(entries.state[batch_idx], entries.action[batch_idx])
        reward_loss = (predicted_reward - entries.reward[batch_idx]).pow(2).mean()
        self.optimizer['reward'].zero_grad()
        reward_loss.backward()
        if self.gradient_clip > 0:
          nn.utils.clip_grad_norm_(self.network.reward_params, self.gradient_clip)
        self.optimizer['reward'].step()
    # Log
    if self.show_tb:
      try:
        self.logger.add_scalar('actor_loss', actor_loss.item(), self.step_count)
        self.logger.add_scalar('IS', ratio.mean().item(), self.step_count)
        self.logger.add_scalar('v', v.mean().item(), self.step_count)
      except:
        pass
      self.logger.add_scalar('critic_loss', critic_loss.item(), self.step_count)
      self.logger.add_scalar('reward_loss', reward_loss.item(), self.step_count)
      self.logger.add_scalar('log_pi', entries.log_pi.mean().item(), self.step_count)
      action_std, _ = self.network.get_entropy_pi(entries.state)
      self.logger.add_scalar('action_std', action_std.mean().item(), self.step_count)
      self.logger.add_scalar('KL', approx_kl.item(), self.step_count)
      self.logger.add_scalar('adv', abs(entries.adv).mean().item(), self.step_count)