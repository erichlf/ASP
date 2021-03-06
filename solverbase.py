__author__ = 'Erich L Foster <erichlf@gmail.com>'
__date__ = '2018-12-26'
__license__ = 'GNU GPL version 3 or any later version'
#
#   adapted from solverbase.py in nsbench originally developed by
#   Anders Logg <logg@simula.no>
#

from dolfin import *
try:
    from dolfin_adjoint import *

    parameters['adjoint"]["record_all'] = True
    adjointer = True
except:
    print('WARNING: Could not import DOLFIN-Adjoint. ' \
        + 'Adjointing will not be available.')
    adjointer = False

from time import time
from os import getpid
from subprocess import getoutput
import sys

# Common solver parameters
maxiter = default_maxiter = 200
tolerance = default_tolerance = 1e-4
nth = ('st', 'nd', 'rd', 'th')  # numerical descriptors


class SolverBase:

    '''
        SolverBase provides a general solver class for the various Solvers. Its
        purpose is take a weak_residual and then solve it using the theta-method
    '''

    def __init__(self, options):

        # Set global DOLFIN and Dolfin-Adjoint parameters
        self.set_parameters(options)
        # set global ASP options
        self.set_options(options)

        # Set log level
        set_log_level(self.log_level)

        # Reset files for storing solution
        self._ufile, self._pfile = None, None
        self._uDualfile, self._pDualfile, self.eifile = None, None, None
        self.meshfile = None
        self.optfile = None

        # Reset storage for functional values and errors
        # Reset some solver variables
        self._t, self._time, self._cputime, self._timestep = [], None, 0.0, 0

    def set_parameters(self, options):

        parameters['form_compiler']['cpp_optimize'] = True
        parameters['allow_extrapolation'] = True
        nonLinearSolver = NewtonSolver()
        prm = nonLinearSolver.parameters
        prm['convergence_criterion'] = 'incremental'
        prm['absolute_tolerance'] = options['absolute_tolerance']
        prm['relative_tolerance'] = options['relative_tolerance']
        prm['report'] = options['monitor_convergence']

        # tell us our refinement strategy
        if 'refinement_algorithm' in options.keys():
            parameters['refinement_algorithm'] = \
                options['refinement_algorithm']
        else:
            parameters['refinement_algorithm'] = 'regular_cut'

    def set_options(self, options):

        self.mem = options['check_mem_usage']

        self.saveSolution = options['save_solution']
        self.saveFrequency = options['save_frequency']

        # initialize the time stepping method parameters
        if 'theta' in options.keys():
            self.theta = options['theta']  # time stepping method
        else:
            self.theta = 0.5

        # adaptivity options
        self.adaptive = options['adaptive']
        self.adaptRatio = options['adapt_ratio']
        self.maxAdapts = options['max_adaptations']
        self.adaptTOL = options['adaptive_TOL']
        self.onDisk = options['on_disk']

        self.dir = options['folder']  # path to save data

        self.optimize = options['optimize']

        self.steady_state = False

        if 'log_level' in options.keys():
            self.log_level = options['log_level']
        else:
            self.log_level = 50  # info

    def solve(self, problem):
        '''
            This is the general solve class which will determine if adaptivity
            should be used or if a problem is an optimization problem.
        '''
        mesh = problem.mesh
        if not self.steady_state:
            T, t0 = problem.T, problem.t0

            # adjust time step so that we evenly divide time interval
            k = self.adjust_dt(t0, T, problem.k)
        else:
            T, t0, k = None, None, None

        if self.adaptive:  # solve with adaptivity
            if adjointer:
                mesh, k = self.adaptivity(problem, mesh, T, t0, k)
            else:
                print('WARNING: You have requested adaptivity, but DOLFIN-Adjoint' \
                    + ' doesn\'t appear to be installed.')
                print('Solving without adaptivity.')

        print('Solving the primal problem.')
        self.file_naming(problem, n=-1, opt=False)

        # record so that we can evaluate our functional
        if adjointer:
            annotate = self.adaptive or (self.optimize and
                                         'Optimize' in dir(problem))
            parameters['adjoint"]["stop_annotating'] = not annotate
        else:
            annotate = False

        func = 'functional' in dir(problem)
        W, w, m = self.forward_solve(problem, mesh, t0, T, k,
                                     func=func, annotate=annotate)

        if m is not None:
            print('The size of the functional is: {:0.3G}'.format(m))

        # solve the optimization problem
        if(self.optimize and 'Optimize' in dir(problem)):
            if adjointer:
                # give me an end line so that dolfin-adjoint doesn't
                # cover previous prints
                print()
                problem.Optimize(self, W, w)

                self.file_naming(problem, n=-1, opt=True)

                parameters['adjoint"]["stop_annotating'] = True
                W, w, m = self.forward_solve(problem, mesh, t0, T, k, func=func)
            else:
                print('WARNING: You have requested Optimization, but' \
                    + ' DOLFIN-Adjoint doesn\'t appear to be installed.')
                print('Not running optimization.')

        return w

    def adaptivity(self, problem, mesh, T, t0, k):
        COND = 1

        # Adaptive loop
        i, m = 0, 0  # initialize
        while(i <= self.maxAdapts and COND > self.adaptTOL):
            # setup file names
            self.file_naming(problem, n=i, opt=False)
            # save our current mesh
            if self.saveSolution:
                self.meshfile << mesh

            print('Solving on {} mesh.'.format(self.which_mesh(i)))

            # Solve primal and dual problems and compute error indicators
            m_ = m  # save the previous functional value
            W, w, m, ei = self.adaptive_solve(problem, mesh, t0, T, k)
            COND = self.condition(ei, m, m_)
            print('DOFs={:d} functional={:0.5G} err_est={:0.5G}'.format(mesh.num_vertices(), m, COND))

            if self.saveSolution:  # Save solution
                self.eifile << ei

            # Refine the mesh
            print('Refining mesh.')
            mesh = self.adaptive_refine(mesh, ei)
            if 'time_step' in dir(problem) and not self.steady_state:
                k = self.adjust_dt(t0, T, problem.time_step(problem.Ubar, mesh))

            adj_reset()  # reset the dolfin-adjoint

            i += 1

        if i > self.maxAdapts and COND > self.adaptTOL:
            print('Warning reached max adaptive iterations with' \
                + 'sum(abs(EI))={:0.3G}. Solution may not be accurate.'.format(COND))

        return mesh, k

    def adaptive_solve(self, problem, mesh, t0, T, k):
        '''
            Adaptive solve applies the error representation to goal-oriented
            adaptivity. This is all done automatically using the weak_residual.
        '''
        print('Solving the primal problem.')
        parameters['adjoint"]["stop_annotating'] = False

        if not self.steady_state:
            N = int(round((T - t0) / k))

            assert self.onDisk <= 1. or self.onDisk >= 0.
            if self.onDisk > 0:
                adj_checkpointing(strategy='multistage', steps=N,
                                  snaps_on_disk=int(self.onDisk * N),
                                  snaps_in_ram=int((1. - self.onDisk) * N),
                                  verbose=False)

        self._timestep = 0  # reset the time step to zero
        W, w, m = self.forward_solve(problem, mesh, t0, T, k, func=True)
        parameters['adjoint"]["stop_annotating'] = True
        self._timestep = 0  # reset the time step to zero

        print('Solving the dual problem.')
        # Generate the dual problem
        phi, wtape = self.compute_dual(problem, W, k, w)

        if self.steady_state:
            self.update(problem, None, W, phi[0], dual=True)
        else:
            print()

        print('Building error indicators.')
        ei = self.build_error_indicators(problem, W, k, phi, wtape)

        return W, w, m, ei

    def compute_dual(self, problem, W, k, w):

        if self.steady_state:
            functional = problem.functional(W, w)
        else:
            functional = problem.functional(W, w) * dt
        J = Functional(functional, name='DualArgument')
        timestep, wtape, phi = None, [], []

        self._timestep = 0  # reset the time step to zero

        # compute the dual solution used in ei and grab the tape value
        t = problem.T
        iteration = 0
        for (adj, var) in compute_adjoint(J, forget=False):
            if var.name == 'w':
                if timestep == var.timestep:
                    iteration += 1
                else:
                    iteration = 0
                timestep = var.timestep
                if not self.steady_state:
                    wtape.append(DolfinAdjointVariable(w, timestep=timestep,
                                                       iteration=iteration).
                                 tape_value())
                else:
                    wtape.append(DolfinAdjointVariable(w).tape_value())
                phi.append(adj)
                if not self.steady_state:
                    self.update(problem, t, W, phi[-1], dual=True)
                    t -= k

        return phi, wtape

    def build_error_indicators(self, problem, W, k, phi, wtape):
        Z = FunctionSpace(W.mesh(), 'DG', 0)
        z = TestFunction(Z)
        ei = Function(Z, name='Error Indicator')
        LR1 = 0.

        if not self.steady_state:
            for i in xrange(len(wtape) - 1):
                # the tape is backwards so i+1 is the previous time step
                wtape_theta = self.theta * wtape[i] \
                    + (1. - self.theta) * wtape[i + 1]
                LR1 = self.weak_residual(problem, Constant(k), W, wtape_theta,
                                         wtape[i], wtape[i + 1], z * phi[i],
                                         ei_mode=True)
                ei.vector()[:] += assemble(LR1, annotate=False).array()
        else:
            LR1 = self.weak_residual(problem, W, wtape[0], z * phi[0],
                                     ei_mode=True)
            ei.vector()[:] = assemble(LR1, annotate=False).array()

        return ei

    def condition(self, ei, m, m_):
        '''
            Adaptive stopping criterion for non-Galerkin-orthogonal problems.
            Overload this for Galerkin-orthogonal problems.
            ei - error indicators (non-Galerkin-orthogonal problems)
            m - current functional size (Galerkin-orthogonal problems)
            m_ - previous functional size (Galerkin-orthogonal problems)
        '''
        c = abs(sum(ei.vector()))

        return c

    def forward_solve(self, problem, mesh, t0, T, k,
                      func=False, annotate=False):
        '''
            Here we take the weak_residual and apply boundary conditions and
            then send it to time_stepper for solving.
        '''

        # Define function spaces
        # we do it this way so that it can be overloaded
        W = self.function_space(mesh)

        if not self.steady_state:
            ic = problem.initial_conditions(W, annotate=annotate)

        # define trial and test function
        wt = TestFunction(W)
        if adjointer:  # only use annotation if DOLFIN-Adjoint was imported
            w = Function(W, name='w')
            if not self.steady_state:
                w_ = Function(ic, name='w_')
        else:
            w = Function(W)
            if not self.steady_state:
                w_ = ic

        if not self.steady_state:
            theta = self.theta
            w_theta = (1. - theta) * w_ + theta * w

            # weak form of the primal problem
            F = self.weak_residual(problem, Constant(k), W, w_theta, w, w_, wt,
                                   ei_mode=False)

            w, m = self.timeStepper(problem, t0, T, k, W, w, w_, F, func=func)
        else:
            # weak form of the primal problem
            F = self.weak_residual(problem, W, w, wt, ei_mode=False)

            w, m = self.steady_solve(problem, W, w, F, func=func)

        return W, w, m

    # define functions spaces
    def function_space(self, mesh):

        print('NO FUNCTION SPACE PROVIDED: You must define a function_space' \
            + ' for this code to work.')
        sys.exit(1)

        return W

    def weak_residual(self, problem, k, W, w, ww, w_, wt, ei_mode=False):

        print('NO WEAK RESIDUAL PROVIDED: You must define a weak_residual for' \
            + ' this code to work.')
        sys.exit(1)

    # Refine the mesh based on error indicators
    def adaptive_refine(self, mesh, ei):
        '''
            Take a mesh and the associated error indicators and refine
            adapt_ratio% of cells.
        '''
        gamma = abs(ei.vector().array())

        # Mark cells for refinement
        cell_markers = MeshFunction('bool', mesh, mesh.topology().dim())
        adapt_n = int(len(gamma) * self.adaptRatio - 1)
        gamma_0 = sorted(gamma, reverse=True)[adapt_n]
        print('Refining {:G} of {:G} cells ({:0.2G}%%).'.format(adapt_n, len(gamma),
                                                                100*adapt_n/len(gamma)))
        for c in cells(mesh):
            cell_markers[c] = gamma[c.index()] > gamma_0

        # Refine mesh
        mesh = refine(mesh, cell_markers)

        return mesh

    def which_mesh(self, i):
        num = map(int, str(i))  # split i so that we can look at last digit
        if i == 0:
            s = 'initial'
        elif num[-1] < len(nth) and (i < 11 or i > 20):
            s = '{:d}{} adapted '.format(i, nth[num[-1] - 1])
        else:
            s = '{:d}{:s} adapted '.format(i, nth[-1])

        return s

    def adjust_dt(self, t0, T, k):
        '''
            Adjust time step so that we evenly divide the time interval, but
            ensure that the new time step is always smaller than the original.
        '''
        d, r = divmod((T - t0), k)
        if r > DOLFIN_EPS:
            k = (T - t0) / (d + 1)

        return k

    def steady_solve(self, problem, W, w, F, func=False):

        self.start_timing()
        bcs = problem.boundary_conditions(W)

        solve(F == 0, w, bcs)

        if func and adjointer:  # annotation only works with DOLFIN-Adjoint
            m = assemble(problem.functional(W, w), annotate=False)
        elif func:
            m = assemble(problem.functional(W, w))
        else:
            m = None

        self.update(problem, None, W, w)

        return w, m

    def timeStepper(self, problem, t, T, k, W, w, w_, F, func=False):
        '''
            Time stepper for solver using theta-method.
        '''
        # Time loop
        self.start_timing()
        if adjointer:
            adj_start_timestep(t)

        bcs = problem.boundary_conditions(W, t)

        # save initial condition
        self.update(problem, t, W, w_)

        if func and adjointer:  # annotation only works with DOLFIN-Adjoint
            m = k * assemble(problem.functional(W, w_), annotate=False)
        elif func:
            m = k * assemble(problem.functional(W, w_))
        else:
            m = None

        while t < T - k / 2.:
            t += k

            if('update' in dir(problem)):
                bcs = problem.update(W, t)

            self.pre_step(problem, t, k, W, w, w_)

            solve(F == 0, w, bcs)

            self.post_step(problem, t, k, W, w, w_)

            w_.assign(w)

            # Determine the value of our functional
            if func and adjointer:  # annotation only works with DOLFIN-Adjoint
                m += k * assemble(problem.functional(W, w_), annotate=False)
            elif func:
                m += k * assemble(problem.functional(W, w_))

            if adjointer:  # can only use if DOLFIN-Adjoint has been imported
                adj_inc_timestep(t, finished=(t > T - k / 2.))

            self.update(problem, t, W, w_)

        print()

        return w, m

    def pre_step(self, problem, t, k, W, w, w_):
        pass

    def post_step(self, problem, t, k, W, w, w_):
        pass

    def update(self, problem, t, W, w, dual=False):
        '''
            Saves the data at each time step.
        '''
        # Add to accumulated CPU time
        timestep_cputime = time() - self._time
        self._cputime += timestep_cputime

        if not dual or t is not None:  # Store time steps
            self._t.append(t)

        if self.saveSolution:  # Save solution
            self.Save(problem, w, dual=dual)

        # Check memory usage
        if self.mem:
            print('Memory usage is:', self.getMyMemoryUsage())

        # Print progress
        if t is not None:
            s = 'Time step {:d} finished in {:g} seconds, '.format(self._timestep,
                                                                   timestep_cputime)
            perc = 100 * t / problem.T
            if dual:
                perc = 100 - perc
                s += '{:g}%% done (t = {:g}, T = {:g}).'.format(perc, round(t, 14),
                                                                problem.T)

        if t is not None:
            sys.stdout.write('\033[K')
            sys.stdout.write(s + '\r')
            sys.stdout.flush()

            # Increase time step
            self._timestep += 1

        # record current time
        self._time = time()

    def Save(self, problem, w, dual=False):
        '''
            Save a variables associated with a time step. Here we assume there
            are two variables where the first variable is vector-valued and the
            second variable is a scalar. If this doesn't fit the particular
            solvers variables the user will need to overload this function.
        '''
        u = w.split()[0]
        p = w.split()[1]

        if self.saveFrequency != 0 \
                and ((self._timestep - 1) % self.saveFrequency == 0
                     or self._timestep == 0 or self._timestep == problem.T):
            if not dual:
                self._ufile << u
                self._pfile << p
            else:
                self._uDualfile << u
                self._pDualfile << p

    def prefix(self, problem):
        '''
            Obtains the beginning of file naming, e.g. Problem Name, Solver
            Name, dimension, etc.
        '''
        # Return file prefix for output files
        p = problem.__module__.split('.')[-1]
        if problem.mesh.topology().dim() > 2:
            p += '3D'
        else:
            p += '2D'
        s = self.__module__.split('.')[-1]

        return problem.output_location + s + p

    def suffix(self, problem):
        '''
            Obtains the run specific data for file naming, e.g. Nx, k, etc.
        '''

        s = 'T' + str(problem.T)
        if problem.Nx is not None:
            s += 'Nx' + str(problem.Nx)
        if problem.Ny is not None:
            s += 'Ny' + str(problem.Ny)
        if problem.mesh.topology().dim() > 2 and problem.Nz is not None:
            s += 'Nz' + str(problem.Nz)

        s += 'K' + str(problem.k)

        return s

    def file_naming(self, problem, n=-1, opt=False):
        '''
            Names our files for saving variables. Here we assume there are
            two variables where the first variable is vector-valued and the
            second variable is a scalar. If this doesn't fit the particular
            solvers variables the user will need to overload this function.
        '''

        s = self.dir + self.prefix(problem) + self.suffix(problem)

        if n == -1:
            if opt:
                self._ufile = File(s + '_uOpt.pvd', 'compressed')
                self._pfile = File(s + '_pOpt.pvd', 'compressed')
            else:
                self._ufile = File(s + '_u.pvd', 'compressed')
                self._pfile = File(s + '_p.pvd', 'compressed')
            self._uDualfile = File(s + '_uDual.pvd', 'compressed')
            self._pDualfile = File(s + '_pDual.pvd', 'compressed')
            self.meshfile = File(s + '_mesh.xml')
        else:  # adaptive specific files
            if self.eifile is None:  # error indicators
                self.eifile = File(s + '_ei.pvd', 'compressed')
                self._ufile = File(s + '_u{:02d}.pvd'.format(n), 'compressed')
            self._pfile = File(s + '_p{:02d}.pvd'.format(n), 'compressed')
            self._uDualfile = File(s + '_uDual{:02d}.pvd'.format(n), 'compressed')
            self._pDualfile = File(s + '_pDual{:02d}.pvd'.format(n), 'compressed')
            self.meshfile = File(s + '_mesh{:02d}.xml'.format(n))

    def getMyMemoryUsage(self):
        '''
            Determines how much memory we are using.
        '''
        mypid = getpid()
        mymemory = getoutput('ps -o rss {}'.format(mypid)).split()[1]
        return mymemory

    def start_timing(self):
        '''
            Start timing, will be paused automatically during update
            and stopped when the end-time is reached.
        '''
        self._time = time()
