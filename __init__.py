__author__ = "Erich L Foster <erichlf@gmail.com>"
__date__ = "2014-10-27"
__license__ = "GNU GPL version 3 or any later version"
__version__ = "0.1"

from dolfin import *
from ASP.solverbase import SolverBase as Solver
from ASP.problembase import ProblemBase as Problem

# Default options
OPTIONS = {
    'dim': 2,  # number of dimensions
    'Nx': 20,  # number of elements along x axis
    'Ny': 20,  # number of elements along y axis
    'Nz': 20,  # number of elements along z axis
    'k': 0.01,  # time-step
    'T': 10.0,  # final-time
    'theta': 0.5,  # time-stepping method
    'stabilize': True,  # stabilize the solution
    'adaptive': False,  # mesh adaptivity
    'refinement_algorithm': 'regular_cut',  # algorithm to use in refinement
    'adapt_ratio': 0.1,  # percent of mesh to refine
    'max_adaptations': 30,  # max number of times to adapt mesh
    'adaptive_TOL': 1E-20,  # tolerance for terminating adaptivity
    'optimize': False,  # optimize as defined in solver
    'on_disk': 0.,  # percent of steps on disk
    'folder': 'results/',  # location to save data
    'save_solution': False,
    'save_frequency': 1,
    'plot_solution': True,
    'debug': False,
    'check_mem_usage': False,
    'absolute_tolerance': 1e-25,
    'relative_tolerance': 1e-12,
    'monitor_convergence': False,
    'initial_mesh': None,  # to use for initial computation
}
