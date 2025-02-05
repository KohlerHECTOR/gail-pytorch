import numpy as np
import torch
import os

from torch.nn import Module
# from torch.utils.tensorboard import SummaryWriter

from models.nets import PolicyNetwork, ValueNetwork, AE
from utils.funcs import get_flat_grads, get_flat_params, set_params, \
    conjugate_gradient, rescale_and_linesearch

if torch.cuda.is_available():
    from torch.cuda import FloatTensor
    torch.set_default_tensor_type(torch.cuda.FloatTensor)
else:
    from torch import FloatTensor


class AEIRL(Module):
    def __init__(
        self,
        state_dim,
        action_dim,
        discrete,
        train_config=None,
        path_save_log="default_save"
    ) -> None:
        super().__init__()

        self.path_save_log = path_save_log
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.discrete = discrete
        self.train_config = train_config

        self.pi = PolicyNetwork(self.state_dim, self.action_dim, self.discrete)
        self.v = ValueNetwork(self.state_dim)

        self.d = AE(self.state_dim, self.action_dim, self.discrete)

    def get_networks(self):
        return [self.pi, self.v]

    def act(self, state, deterministic=False):
        self.pi.eval()
        state = FloatTensor(state)
        if deterministic:
            return self.pi(state, deterministic=True).detach().cpu().numpy()

        distb = self.pi(state)

        action = distb.sample().detach().cpu().numpy()

        return action

    def eval_pol(self, env, nb_eval=10, nb_step_eval=10000):

        eval = 0
        for n in range(nb_eval):
            env.seed(n)
            s = env.reset()
            reward = 0

            for t in range(nb_step_eval):
                with torch.no_grad():
                    action = self.act(s, deterministic=True)

                next_state, r, done, _ = env.step(action)

                s = next_state
                reward += r

                if done:
                    break
            eval += reward

        print("EVAL REWARD : {}".format(eval/nb_eval))
        return eval/nb_eval

    def train(self, env, expert, render=False, noise=0):
        print("NOISE", noise)
        num_iters = self.train_config["num_iters"]
        num_steps_per_iter = self.train_config["num_steps_per_iter"]
        horizon = self.train_config["horizon"]
        lambda_ = self.train_config["lambda"]
        gae_gamma = self.train_config["gae_gamma"]
        gae_lambda = self.train_config["gae_lambda"]
        eps = self.train_config["epsilon"]
        max_kl = self.train_config["max_kl"]
        cg_damping = self.train_config["cg_damping"]
        normalize_advantage = self.train_config["normalize_advantage"]
        nb_eval = self.train_config["nb_eval"]
        nb_step_eval = self.train_config["nb_step_eval"]
        eval_freq = self.train_config["eval_freq"]

        env_name = env.unwrapped.spec.id
        method = 'aeirl'

        if not os.path.exists(self.path_save_log):
            os.mkdir(self.path_save_log)

        with open(self.path_save_log+'/'+method+'.txt', 'a') as f:
            f.write('NEW Sim : \n')
        with open(self.path_save_log+'/'+method+'_eval.txt', 'a') as f:
            f.write('NEW Sim : \n')
        # writer = SummaryWriter(f"runs/{env_name}")

        opt_d = torch.optim.Adam(self.d.parameters())

        exp_rwd_iter = []

        exp_obs = []
        exp_acts = []

        steps = 0

############ EXPERT DATA #######################################################
        while steps < num_steps_per_iter:
            ep_obs = []
            ep_rwds = []

            t = 0
            done = False

            ob = env.reset()

            while not done and steps < num_steps_per_iter:
                if env_name in ["Hopper-v2", "Swimmer-v2", "Walker2d-v2", "Reacher-v2"]:
                    act = expert.predict(ob, deterministic=True)[0]
                else:
                    act = expert.act(ob)

                ep_obs.append(ob + np.random.normal(0, noise, self.state_dim))
                exp_obs.append(ob + np.random.normal(0, noise, self.state_dim))
                exp_acts.append(
                    act + np.random.normal(0, noise, self.action_dim))

                if render:
                    env.render()
                ob, rwd, done, info = env.step(act)

                ep_rwds.append(rwd)

                t += 1
                steps += 1

                if horizon is not None:
                    if t >= horizon:
                        done = True
                        break

            if done:
                exp_rwd_iter.append(np.sum(ep_rwds))

            ep_obs = FloatTensor(np.array(ep_obs))
            ep_rwds = FloatTensor(ep_rwds)

        exp_rwd_mean = np.mean(exp_rwd_iter)
        # print(
        #     "Expert Reward Mean: {}".format(exp_rwd_mean)
        # )

        exp_obs = FloatTensor(np.array(exp_obs))
        exp_acts = FloatTensor(np.array(exp_acts))
################################################################################


########### MAIN LOOP###########################################################
        rwd_iter_means = []
        for i in range(num_iters):
            rwd_iter = []

            obs = []
            acts = []
            rets = []
            advs = []
            gms = []

            steps = 0
            while steps < num_steps_per_iter:
                ep_obs = []
                ep_acts = []
                ep_rwds = []
                ep_costs = []
                ep_disc_costs = []
                ep_gms = []
                ep_lmbs = []

                t = 0
                done = False

                ob = env.reset()

                while not done and steps < num_steps_per_iter:
                    act = self.act(ob)

                    ep_obs.append(ob)
                    obs.append(ob)

                    ep_acts.append(act)
                    acts.append(act)

                    if render:
                        env.render()
                    ob, rwd, done, info = env.step(act)

                    ep_rwds.append(rwd)
                    ep_gms.append(gae_gamma ** t)
                    ep_lmbs.append(gae_lambda ** t)

                    t += 1
                    steps += 1

                    if horizon is not None:
                        if t >= horizon:
                            done = True
                            break

                if done:
                    rwd_iter.append(np.sum(ep_rwds))

                ep_obs = FloatTensor(np.array(ep_obs))
                ep_acts = FloatTensor(np.array(ep_acts))
                ep_rwds = FloatTensor(ep_rwds)
                # ep_disc_rwds = FloatTensor(ep_disc_rwds)
                ep_gms = FloatTensor(ep_gms)
                ep_lmbs = FloatTensor(ep_lmbs)

                ep_costs = 1 / (1 + self.d(ep_obs, ep_acts).squeeze().detach())

                ep_disc_costs = ep_gms * ep_costs

                ep_disc_rets = FloatTensor(
                    [sum(ep_disc_costs[i:]) for i in range(t)]
                )
                ep_rets = ep_disc_rets / ep_gms

                rets.append(ep_rets)

                self.v.eval()
                curr_vals = self.v(ep_obs).detach()
                next_vals = torch.cat(
                    (self.v(ep_obs)[1:], FloatTensor([[0.]]))
                ).detach()
                ep_deltas = ep_costs.unsqueeze(-1)\
                    + gae_gamma * next_vals\
                    - curr_vals

                ep_advs = FloatTensor([
                    ((ep_gms * ep_lmbs)[:t - j].unsqueeze(-1) * ep_deltas[j:])
                    .sum()
                    for j in range(t)
                ])
                advs.append(ep_advs)

                gms.append(ep_gms)

            rwd_iter_means.append(np.mean(rwd_iter))
            # print(
            #     "Iterations: {},   Reward Mean: {}"
            #     .format(i + 1, np.mean(rwd_iter))
            # )
            # writer.add_scalars(f'reward', {
            #     'expert': exp_rwd_mean,
            #     'aeirl': np.mean(rwd_iter),
            # }, i)

            obs = FloatTensor(np.array(obs))
            acts = FloatTensor(np.array(acts))
            rets = torch.cat(rets)
            advs = torch.cat(advs)
            gms = torch.cat(gms)

            if normalize_advantage:
                advs = (advs - advs.mean()) / advs.std()

            self.d.train()
            exp_scores = 1 / (1 + self.d.get_logits(exp_obs, exp_acts))
            nov_scores = 1 / (1 + self.d.get_logits(obs, acts))

            opt_d.zero_grad()
            loss = nov_scores.mean() - exp_scores.mean()
            # writer.add_scalar('Loss_AE_AEIRL', loss.item(), i)

            loss.backward()
            opt_d.step()

            self.v.train()
            old_params = get_flat_params(self.v).detach()
            old_v = self.v(obs).detach()

            def constraint():
                return ((old_v - self.v(obs)) ** 2).mean()

            grad_diff = get_flat_grads(constraint(), self.v)

            def Hv(v):
                hessian = get_flat_grads(torch.dot(grad_diff, v), self.v)\
                    .detach()

                return hessian

            g = get_flat_grads(
                ((-1) * (self.v(obs).squeeze() - rets) ** 2).mean(), self.v
            ).detach()
            s = conjugate_gradient(Hv, g).detach()

            Hs = Hv(s).detach()
            alpha = torch.sqrt(2 * eps / torch.dot(s, Hs))

            new_params = old_params + alpha * s

            set_params(self.v, new_params)

            self.pi.train()
            old_params = get_flat_params(self.pi).detach()
            old_distb = self.pi(obs)

            def L():
                distb = self.pi(obs)

                return (advs * torch.exp(
                    distb.log_prob(acts)
                    - old_distb.log_prob(acts).detach()
                )).mean()

            def kld():
                distb = self.pi(obs)

                if self.discrete:
                    old_p = old_distb.probs.detach()
                    p = distb.probs

                    return (old_p * (torch.log(old_p) - torch.log(p)))\
                        .sum(-1)\
                        .mean()

                else:
                    old_mean = old_distb.mean.detach()
                    old_cov = old_distb.covariance_matrix.sum(-1).detach()
                    mean = distb.mean
                    cov = distb.covariance_matrix.sum(-1)

                    return (0.5) * (
                        (old_cov / cov).sum(-1)
                        + (((old_mean - mean) ** 2) / cov).sum(-1)
                        - self.action_dim
                        + torch.log(cov).sum(-1)
                        - torch.log(old_cov).sum(-1)
                    ).mean()

            grad_kld_old_param = get_flat_grads(kld(), self.pi)

            def Hv(v):
                hessian = get_flat_grads(
                    torch.dot(grad_kld_old_param, v),
                    self.pi
                ).detach()

                return hessian + cg_damping * v

            g = get_flat_grads(L(), self.pi).detach()

            s = conjugate_gradient(Hv, g).detach()
            Hs = Hv(s).detach()

            new_params = rescale_and_linesearch(
                g, s, Hs, max_kl, L, kld, old_params, self.pi
            )

            set_params(self.pi, new_params)

            with torch.no_grad():
                trpo_loss = L()

            with open(self.path_save_log+'/'+method+'.txt', 'a') as f:
                f.write(str(i)+',' + str(exp_rwd_mean)+',' +
                        str(trpo_loss.item())+','+str(loss.item())+'\n')

            if (i + 1) % eval_freq == 0 or i == 0:
                with open(self.path_save_log+'/'+method+'_eval.txt', 'a') as f:
                    f.write(
                        str(i)+','+str(self.eval_pol(env, nb_eval, nb_step_eval)) + '\n')

        return exp_rwd_mean, rwd_iter_means
