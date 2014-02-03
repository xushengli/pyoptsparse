#from __future__ import absolute_import
#/bin/env python
'''
pyIPOPT - A python wrapper to the core IPOPT compiled module. 

Copyright (c) 2013-2014 by Dr. Gaetan Kenway
All rights reserved.

Tested on:
---------
Linux with intel

Developers:
-----------
- Dr. Gaetan Kenway (GKK)
- Dr. Graeme Kennedy (GJK)
History
-------
    v. 0.1    - Initial Wrapper Creation 
'''
# =============================================================================
# IPOPT Library
# =============================================================================
#from . import pyipoptcore
import pyipoptcore
# try:
#     import pyipoptcore
# except:
#     raise ImportError('IPOPT shared library failed to import')

# =============================================================================
# Standard Python modules
# =============================================================================
import os
import sys
import copy
import time
import types
# =============================================================================
# External Python modules
# =============================================================================
import numpy
import shelve
from scipy import sparse
# # =============================================================================
# # Extension modules
# # =============================================================================
from ..pyOpt_optimizer import Optimizer
from ..pyOpt_history import History
from ..pyOpt_gradient import Gradient
from ..pyOpt_solution import Solution
from ..pyOpt_error import Error
# =============================================================================
# Misc Definitions
# =============================================================================
inf = 1e20  # define a value for infinity

# Try to import mpi4py and determine rank
try: 
    from mpi4py import MPI
    rank = MPI.COMM_WORLD.rank
except:
    rank = 0
    MPI = None
# end try

# =============================================================================
# IPOPT Optimizer Class
# =============================================================================
class IPOPT(Optimizer):
    '''
    IPOPT Optimizer Class - Inherited from Optimizer Abstract Class
    '''
    
    def __init__(self, *args, **kwargs):
        '''
        IPOPT Optimizer Class Initialization
        '''
        
        name = 'IPOPT'
        category = 'Local Optimizer'
        def_opts = {'tol':[float,1e-6],
                    'hessian_approximation':[str,'limited-memory'],
                    'limited_memory_max_history':[int,10],
                    'max_iter':[int,100],
                    # print options
                    'print_level':[int, 5], # Output verbosity level. '0-12'
                    'print_user_options':[str,'no'], #yes or no, Print all options set by the user. 
                    'print_options_documentation':[str,'no'],#yes or no,Switch to print all algorithmic options. 
                    'print_frequency_iter':[int,1],# Determines at which iteration frequency the summarizing iteration output line should be printed. 
                    'print_frequency_time':[int,0],# Determines at which time frequency the summarizing iteration output line should be printed. could be float??
                    'output_file':[str,'IPOPT_print.out'],
                    'file_print_level':[int,5],#Verbosity level for output file. '0-12'
                    'option_file_name':[str,'IPOPT_options.opt'],
                    'print_info_string':[str,'no'],#yes or no.Enables printing of additional info string at end of iteration output. 
                    'inf_pr_output':[str,'original'],#Determines what value is printed in the "inf_pr" output column. 'internal' or 'original'
                    'print_timing_statistics':[str,'no'],#yes or no
                    # Derivative Testing options
                    'derivative_test':[str,'none'], # none,first-order,second-order,only-second-order
                    'derivative_test_perturbation':[float,1e-8],
                    'derivative_test_tol':[float,1e-4],
                    'derivative_test_print_all':[str,'no'],#yes,no
                    'derivative_test_first_index':[int,-2],
                    'point_perturbation_radius':[int,10], #might be a float
                    }
        informs = { # Don't have any of these yet either..
            }

        self.set_options = []
        Optimizer.__init__(self, name, category, def_opts, informs, *args, **kwargs)

        # IPOPT needs jacobians in coo format
        self.jacType = 'coo'

        # Constrained until we know otherwise :-)
        self.unconstrained = False

    def __call__(self, optProb, sens=None, sensStep=None, sensMode=None,
                  storeHistory=None, hotStart=None, 
                  coldStart=None, timeLimit=None, comm=None):
        '''
        This is the main routine used to solve the optimization
        problem.

        Parameters
        ----------
        optProb : Optimization or Solution class instance
            This is the complete description of the optimization problem
            to be solved by the optimizer

        sens : str or python Function.
            Specifiy method to compute sensitivities.  To explictly
            use pyOptSparse gradient class to do the derivatives with
            finite differenes use \'FD\'. \'sens\' may also be \'CS\'
            which will cause pyOptSpare to compute the derivatives
            using the complex step method. Finally, \'sens\' may be a
            python function handle which is expected to compute the
            sensitivities directly. For expensive function evaluations
            and/or problems with large numbers of design variables
            this is the preferred method.

        sensStep : float 
            Set the step size to use for design variables. Defaults to
            1e-6 when sens is \'FD\' and 1e-40j when sens is \'CS\'. 

        sensMode : str
            Use \'pgc\' for parallel gradient computations. Only
            available with mpi4py and each objective evaluation is
            otherwise serial
            
        storeHistory : str
            File name of the history file into which the history of
            this optimization will be stored

        hotStart : str
            File name of the history file to "replay" for the
            optimziation.  The optimization problem used to generate
            the history file specified in \'hotStart\' must be
            **IDENTICAL** to the currently supplied \'optProb\'. By
            identical we mean, **EVERY SINGLE PARAMETER MUST BE
            IDENTICAL**. As soon as he requested evaluation point does
            not match the history, function and gradient evaluations
            revert back to normal evaluations.
             
        coldStart : str
            Filename of the history file to use for "cold"
            restart. Here, the only requirment is that the number of
            design variables (and their order) are the same. Use this
            method if any of the optimization parameters have changed.

        timeLimit : number
            Number of seconds to run the optimization before a
            terminate flag is given to the optimizer and a "clean"
            exit is performed.

        comm : MPI Intra communicator
            Specifiy a MPI comm to use. Default is None. If mpi4py is not
            available, the serial mode will still work. if mpi4py *is*
            available, comm defaluts to MPI.COMM_WORLD. 
            '''
        
        self.callCounter = 0

        if len(optProb.constraints) == 0:
            # If the user *actually* has an unconstrained problem,
            # snopt sort of chokes with that....it has to have at
            # least one constraint. So we will add one
            # automatically here:
            self.unconstrained = True
            optProb.dummyConstraint = True

        # Save the optimization problem and finialize constraint
        # jacobian, in general can only do on root proc
        self.optProb = optProb
        self.optProb.finalizeDesignVariables()
        self.optProb.finalizeConstraints()
        if self.optProb.nlCon > 0:
            self.appendLinearConstraints = True

        # Setup initial cache values
        self._setInitialCacheValues()
        self._setSens(sens, sensStep, sensMode, comm)
              
        # We make a split here: If the rank is zero we setup the
        # problem and run IPOPT, otherwise we go to the waiting loop:

        if rank == 0:
            blx, bux, xs = self._assembleContinuousVariables()
            ncon, blc, buc = self._assembleConstraints()

            # Before we start, we assemble the full jacobian, convert
            # to COO format, and store the format since we will need
            # that on the first constraint jacobian call back. 
            # -----------------------------------------------------
            # Get nonlinear part:
            gcon = {}
            for iCon in self.optProb.constraints:
                con = self.optProb.constraints[iCon]
                if not con.linear:
                    gcon[iCon] = con.jac

            fullJacobian = self.optProb.processConstraintJacobian(
                gcon, linearFlag=False)

            # If we have linear constraints those are already done actually.
            if self.optProb.linearJacobian is not None:
                fullJacobian = sparse.vstack([fullJacobian,
                                              self.optProb.linearJacobian])

            # Now what we need for IPOPT is precisely the .row and
            # .col attributes of the fullJacobian array
            matStruct = (fullJacobian.row.copy().astype('int_'), 
                         fullJacobian.col.copy().astype('int_'))

            self._setHistory(storeHistory)
            self._hotStart(storeHistory, hotStart)

            # Define the 4 call back functions that ipopt needs:
            def eval_f(x, user_data=None):
                fobj, fail = self.masterFunc(x, ['fobj'])
                return fobj

            def eval_g(x, user_data = None):
                fcon, fail = self.masterFunc(x, ['fcon'])
                return fcon.copy()

            def eval_grad_f(x, user_data= None):
                gobj, fail = self.masterFunc(x, ['gobj'])
                return gobj.copy()

            def eval_jac_g(x, flag, user_data = None):
                if flag:
                    return copy.deepcopy(matStruct)
                else:
                    gcon, fail = self.masterFunc(x, ['gcon'])
                    return gcon.copy()
            
            timeA = time.time()
            nnzj = len(matStruct[0])
            nnzh = 0
            nlp = pyipoptcore.create(len(xs), blx, bux, ncon, blc, buc, nnzj, nnzh, 
                                          eval_f, eval_grad_f, eval_g, eval_jac_g) 
            
            self._set_ipopt_options(nlp)
            x, zl, zu, constraint_multipliers, obj, status = nlp.solve(xs)
            nlp.close()
            optTime = time.time()-timeA
            
            if self.storeHistory:
                self.hist.close()

            # Store Results
            sol_inform = {}
            # sol_inform['value'] = inform
            # sol_inform['text'] = self.informs[inform[0]]

            # Create the optimization solution
            funcEval = 1
            sol = self._createSolution(optTime, funcEval, sol_inform, obj)

            if MPI:
                # Broadcast a -1 to indcate IPOPT has finished
                MPI.COMM_WORLD.bcast(-1, root=0)

        else:
            self.waitLoop()
            sol = None
        # end if

        # Communicate the solution -- We are back to the point where
        # all processors are back together, so a standard bcast is
        # fine.
        if MPI:
            sol = MPI.COMM_WORLD.bcast(sol)

        return  sol
    
    def _set_ipopt_options(self,nlp):
        '''
        set all of the the options in self.set_options in the ipopt instance nlp
        '''
        # Set Options from the local options dictionary
        # ---------------------------------------------

        for item in self.set_options:
            name = item[0]
            value = item[1]
            print 'value',name,value
            if isinstance(value, str):
                nlp.str_option(name,value)
            elif isinstance(value, float):
                nlp.num_option(name,value)
            elif isinstance(value, int):
                nlp.int_option(name,value)
            else:
                print 'invalid option type',type(value)
            # end
        # end for

        return

    def _on_setOption(self, name, value):
        '''
        Set Optimizer Option Value (Optimizer Specific Routine)
        
        Documentation last updated:  May. 07, 2008 - Ruben E. Perez
        '''
        
        self.set_options.append([name,value])
        
    def _on_getOption(self, name):
        '''
        Get Optimizer Option Value (Optimizer Specific Routine)
        
        Documentation last updated:  May. 07, 2008 - Ruben E. Perez
        '''
        
        pass
        
    def _on_getInform(self, infocode):
        '''
        Get Optimizer Result Information (Optimizer Specific Routine)
        
        Keyword arguments:
        -----------------
        id -> STRING: Option Name
        
        Documentation last updated:  May. 07, 2008 - Ruben E. Perez
        '''
        
        # 
        mjr_code = (infocode[0]/10)*10
        mnr_code = infocode[0] - 10*mjr_code
        try:
            inform_text = self.informs[mjr_code]
        except:
            inform_text = 'Unknown Exit Status'
        # end try
        
        return inform_text
        
    def _on_flushFiles(self):
        '''
        Flush the Output Files (Optimizer Specific Routine)
        
        Documentation last updated:  August. 09, 2009 - Ruben E. Perez
        '''
        
        pass
 
#==============================================================================
# IPOPT Optimizer Test
#==============================================================================
if __name__ == '__main__':
    
    ipopt = IPOPT()
    print ipopt
