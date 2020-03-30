import jax
from jax.experimental.ode import build_odeint
import jax.numpy as np

def build_odeint_batch(dx_dt, **kwargs):
    '''
    Build a vectorized ODE solver
    
    Avoids current issue with applying vmap to odeint
    '''
    def dx_dt_batch(x, t, *args):
        '''
        Time derivative for a flattened batch of inputs to dx_dt
        
        x has shape (batch_sz * d,)
        t is a scalar
        other args have shape (batch_sz,) 
        
        Strategy: reshape x to (batch_sz, d) and call vmap(dx_dt)(...),
        then reshape derivative back to (batch_sz * d)
        '''
        batch_sz = len(args[0])
        x = x.reshape(batch_sz, -1)
        d = jax.vmap(dx_dt, in_axes=(0, None) + (0,) * len(args))(x, t, *args)
        return d.ravel()
    
    ode = build_odeint(dx_dt_batch, **kwargs)
    
    def odeint_batch(x0, t, *args):
        batch_sz, d = x0.shape
        x = ode(x0.ravel(), t, *args)
        # result is (T, batch_sz, d). massage to (batch_sz, T, d)
        return x.reshape(len(t), batch_sz, d).swapaxes(0,1)

    return odeint_batch


class CompartmentModel(object):
    '''
    Base class for compartment models
    '''
    
    def dx_dt(self, x, *args):
        '''Compute time derivative'''
        raise NotImplementedError()
        return

    
    def __init__(self, rtol=1e-5, atol=1e-3, mxstep=500):
        
        self.odeint = build_odeint(self.dx_dt, 
                                   rtol=rtol,
                                   atol=atol, 
                                   mxstep=mxstep)
        
        self.batch_odeint = build_odeint_batch(self.dx_dt,
                                               rtol=rtol,
                                               atol=atol,
                                               mxstep=mxstep)
    
    
    def run(self, T, x0, theta):
        
        # Theta is a tuple of parameters. Entries are 
        # scalars or vectors of length T-1
        is_scalar = [np.ndim(a)==0 for a in theta]
        if np.all(is_scalar):
            return self._run_static(T, x0, theta)        
        else:
            return self._run_time_varying(T, x0, theta)
            
    
    def _run_static(self, T, x0, theta):
        '''
        x0 is shape (d,)
        theta is shape (nargs,)
        '''
        t = np.arange(T, dtype='float32')
        return self.odeint(x0, t, *theta)

    
    def _run_time_varying(self, T, x0, theta):
        
        theta = tuple(np.broadcast_to(a, (T-1,)) for a in theta)

        '''
        x0 is shape (d,)
        theta is shape (nargs, T-1)
        '''
        t_one_step = np.array([0.0, 1.0])
        
        def advance(x0, theta):
            x1 = self.odeint(x0, t_one_step, *theta)[1]
            return x1, x1

        # Run T–1 steps of the dynamics starting from the intial distribution
        _, X = jax.lax.scan(advance, x0, theta, T-1)
        return np.vstack((x0, X))
    
    
    def run_batch(self, T, x0, theta):
        '''
        Run dynamics for a batch of (x0, theta) pairs
    
        x0 is shape (batch_sz, d)
        entries of theta are either (batch_sz,) or (batch_sz, T-1)
        '''
        
        batch_sz, d = x0.shape
        
        '''
        For jax.lax.scan, entries of theta must have size (T-1, batch_sz)
        '''
        def expand_and_transpose(a):
            return np.broadcast_to(a.T, (T-1, batch_sz))
            
        theta = tuple(expand_and_transpose(a) for a in theta)
        
        t_one_step = np.array([0.0, 1.0])
        
        def advance(x0, theta):
            x1 = self.batch_odeint(x0, t_one_step, *theta)[:,-1,:]
            return x1, x1
        
        _, X = jax.lax.scan(advance, x0, theta, T-1)  # (T-1, batch_sz, d)
        
        X = X.swapaxes(0, 1) # --> (batch_sz, T-1, d)
        
        X = np.concatenate((x0[:,None,:], X), axis=1)
        
        return X

    

class SIRModel(CompartmentModel):

    def dx_dt(self, x, t, beta, gamma):
        """
        SIR equations
        """        
        S, I, R, C = x
        N = S + I + R
        
        dS_dt = - beta * S * I / N
        dI_dt = beta * S * I / N - gamma * I
        dR_dt = gamma * I
        dC_dt = beta * S * I / N  # cumulative infections
        
        return np.stack([dS_dt, dI_dt, dR_dt, dC_dt])

    @staticmethod
    def R0(theta):
        beta, gamma = theta
        return beta/gamma
    
    @staticmethod
    def growth_rate(theta):
        beta, gamma = theta
        return beta - gamma

    @staticmethod
    def seed(N=1e6, I=100.):
        '''
        Seed infection. Return state vector for I infected out of N
        '''
        return np.stack([N-I, I, 0.0, I])
        


class SEIRModel(CompartmentModel):
    
    def dx_dt(self, x, t, beta, sigma, gamma):
        """
        SEIR equations
        """        
        S, E, I, R, C = x
        N = S + E + I + R
        
        dS_dt = - beta * S * I / N
        dE_dt = beta * S * I / N - sigma * E
        dI_dt = sigma * E - gamma * I
        dR_dt = gamma * I
        dC_dt = sigma * E  # cumulative infections
        
        return np.stack([dS_dt, dE_dt, dI_dt, dR_dt, dC_dt])

    @staticmethod
    def R0(theta):
        beta, sigma, gamma = theta
        return beta / gamma
    
    @staticmethod
    def growth_rate(theta):
        '''
        Initial rate of exponential growth
        
        Reference: Junling Ma, Estimating epidemic exponential growth rate 
        and basic reproduction number, Infectious Disease Modeling, 2020
        '''
        beta, sigma, gamma = theta
        return (-(sigma + gamma) + np.sqrt((sigma - gamma)**2 + 4 * sigma * beta))/2.
        
    @staticmethod
    def seed(N=1e6, I=100., E=0.):
        '''
        Seed infection. Return state vector for I exponsed out of N
        '''
        return np.stack([N-E-I, E, I, 0.0, I])
