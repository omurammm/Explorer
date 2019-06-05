import math
import torch
import random


class BaseExploration(object):
  # Base class for agent exploration strategies. 
  def __init__(self, exploration):
    self.exploration_steps = exploration['exploration_steps']

  def select_action(self, q_values):
    raise NotImplementedError("To be implemented")


class EpsilonGreedy(BaseExploration):
  '''
  Implementation of epsilon greedy exploration strategy
  '''
  def __init__(self, exploration):
    super().__init__(exploration)
    self.epsilon = exploration['epsilon']

  def select_action(self, q_values, step_count):
    if random.random() < self.epsilon or step_count <= self.exploration_steps:
      action = random.randint(0, q_values.shape[1] - 1)
    else:
      action = torch.argmax(q_values).item()
    return action


class LinearEpsilonGreedy(BaseExploration):
  '''
  Implementation of linear decay epsilon greedy exploration strategy
  '''
  def __init__(self, exploration):
    super().__init__(exploration)
    self.inc = (exploration['end'] - exploration['start']) / float(exploration['steps'])
    self.start = exploration['start']
    self.end = exploration['end']
    if exploration['end'] > exploration['start']:
      self.bound = min
    else:
      self.bound = max

  def select_action(self, q_values, step_count):
    self.epsilon = self.bound(self.start + step_count * self.inc, self.end)
    if random.random() < self.epsilon or step_count <= self.exploration_steps:
      action = random.randint(0, q_values.shape[1] - 1)
    else:
      action = torch.argmax(q_values).item()
    return action


class ExponentialEpsilonGreedy(BaseExploration):
  '''
  Implementation of exponential decay epsilon greedy exploration strategy:
    epsilon = bound(epsilon_end, epsilon_start * exp(- decay * step))
  '''
  def __init__(self, exploration):
    super().__init__(exploration)
    self.decay = exploration['decay']
    self.start = exploration['start']
    self.end = exploration['end']
    if exploration['end'] > exploration['start']:
      self.bound = min
    else:
      self.bound = max

  def select_action(self, q_values, step_count):
    self.epsilon = self.bound(self.start * math.exp(-self.decay * step_count), self.end)
    if random.random() < self.epsilon or step_count <= self.exploration_steps:
      action = random.randint(0, q_values.shape[1] - 1)
    else:
      action = torch.argmax(q_values).item()
    return action