import pyopencl as cl
import numpy as np

from firedrake import SpatialCoordinate, Function, \
    VectorFunctionSpace
from firedrake.functionspaceimpl import WithGeometry

import firedrake_to_pytential as fd_to_pyt

from pytential import bind
from pytential.target import PointsTarget
from warnings import warn


class FunctionConverter:
    """
        This class acts as a manager to generically convert
        :mod:`firedrake` :class:`Function`s to meshmode.
    """
    def __init__(self, cl_ctx, **kwargs):
        """
        :kwargs: These are for the :class:`FiredrakeMeshmodeConverter`,
                 used in the construction of a :mod:`pytential`
                 :class:`QBXLayerPotentialSource`
        """
        self._converters = []
        self._dg_fspace_analogs = []
        self._mesh_analogs = []
        self._finat_element_analogs = []
        self._cell_analogs = []

        self._cl_ctx = cl_ctx
        self._kwargs = kwargs

    def get_converter(self, function_or_space, bdy_id=None):
        space = function_or_space
        if isinstance(space, Function):
            space = function_or_space.function_space()

        # See if already have a converter
        for conv in self._converters:
            if conv.can_convert(space, bdy_id):
                return conv

        def check_for_analog(analog_list, obj):
            for pos_analog in analog_list:
                if pos_analog.is_analog(obj):
                    return pos_analog
            return None

        # See if have a dg space analog
        dg_fspace_analog = check_for_analog(self._dg_fspace_analogs, space)

        # If not, construct one
        if dg_fspace_analog is None:

            # Check for mesh analog and construct if necessary
            mesh_analog = check_for_analog(self._mesh_analogs, space.mesh())
            if mesh_analog is None:
                mesh_analog = fd_to_pyt.MeshAnalog(space.mesh())
                self._mesh_analogs.append(mesh_analog)

            # Check for cell analog and construct if necessary
            cell_analog = check_for_analog(self._cell_analogs,
                                           space.finat_element.cell)
            if cell_analog is None:
                cell_analog = fd_to_pyt.SimplexCellAnalog(space.finat_element.cell)
                self._cell_analogs.append(cell_analog)

            # Check for finat element analog and construct if necessary
            finat_element_analog = check_for_analog(self._finat_element_analogs,
                                                    space.finat_element)
            if finat_element_analog is None:
                finat_element_analog = fd_to_pyt.FinatElementAnalog(
                    space.finat_element, cell_analog)
                self._finat_element_analogs.append(finat_element_analog)

            # Construct dg fspace analog
            dg_fspace_analog = fd_to_pyt.DGFunctionSpaceAnalog(
                mesh_analog, finat_element_analog, cell_analog)

            self._dg_fspace_analogs.append(dg_fspace_analog)

        conv = fd_to_pyt.FiredrakeMeshmodeConverter(self._cl_ctx,
                                                    dg_fspace_analog,
                                                    bdy_id=bdy_id,
                                                    **self._kwargs)
        self._converters.append(conv)

        return conv

    def convert(self, queue, function, firedrake_to_meshmode=True,
                bdy_id=None, put_on_array=False):
        """
            output is a :mod:`numpy` :class:`ndarray`, or a 
            pyopencl.array.Array if put_on_array is *True*
        """
        converter = self.get_converter(function, bdy_id)
        result = converter.convert(queue, function.dat.data,
                                   firedrake_to_meshmode=firedrake_to_meshmode)

        if put_on_array:
            result = cl.array.to_device(queue, result)

        return result

    def get_qbx(self, function_or_space, bdy_id=None):
        converter = self.get_converter(function_or_space, bdy_id)
        return converter._source_qbx

    def get_meshmode_mesh(self, function_or_space, bdy_id=None):
        return self.get_qbx(function_or_space, bdy_id).density_discr.mesh


class OpConnection:
    """
        The idea is to make an easier interface for defining
        operations quickly.
        Rather than have the user manage all of the convergence,
        simply evaluate an operation on a firedrake mesh or
        boundary points
    """

    # TODO : Make these args or kwargs or something
    def __init__(self, function_converter, op, from_fspace,
                 out_fspace,
                 targets=None, source_bdy_id=None):
        # TODO : Explain that targets as *None* is allowable in docs
        """
            :arg targets:
             - an *int*, the target will be the
               boundary ids at :arg:`targets`.
             - If an iterable of *int* types, then
               the target will be any boundary which
               has one of the given ids

             WARNING: Currently, only exterior facet ids are
                      supported

             WARNING: In either case, the pytential
                      op is just passed a collection of points
                      for evaluation. In particular, any attempt
                      at normal derivative evaluation along
                      the target mesh will fail.

            For other args, see :class:`FiredrakeMeshmodeConnection`
        """
        # {{{ Handle targets
        out_mesh = out_fspace.mesh()

        self.target_indices = None
        if targets is not None:
            # if just passed an int, convert to an iterable of ints
            # so that just one case to deal with
            if isinstance(targets, int):
                targets = [targets]
            target_markers = set(targets)

            # Check that boundary ids are valid
            if not target_markers <= set(out_mesh.exterior_facets.unique_markers):
                warn("The following boundary ids are not exterior facet ids: %s" %
                     (target_markers - set(out_mesh.exterior_facets.unique_markers)))

            if not target_markers & set(out_mesh.exterior_facets.unique_markers):
                raise ValueError("No boundary ids are exterior facet ids")

            self.target_indices = set()
            for marker in target_markers:
                self.target_indices |= set(
                    out_fspace.boundary_nodes(marker, 'geometric'))
            self.target_indices = np.array(list(self.target_indices), dtype=np.int32)

            # Get coordinates of nodes
            xx = SpatialCoordinate(out_mesh)
            function_space_dim = VectorFunctionSpace(
                out_mesh,
                out_fspace.ufl_element().family(),
                degree=out_fspace.ufl_element().degree())

            coords = Function(function_space_dim).interpolate(xx)
            coords = np.real(coords.dat.data)

            target_pts = coords[self.target_indices]
            # change from [nnodes][ambient_dim] to [ambient_dim][nnodes]
            target_pts = np.transpose(target_pts).copy()
            self.target = PointsTarget(target_pts)
        else:
            target_qbx = function_converter.get_qbx(out_fspace)
            self.target = target_qbx.density_discr

        # }}}

        self._bound_op = None

        self._out_fspace = out_fspace
        self._bdy_id = source_bdy_id
        self.function_converter = function_converter

        self.set_op(op, from_fspace)

    def set_op(self, op, function_or_space):
        # FIXME : If no boundary id given, then make target the discretization
        #         of the whole mesh!!
        qbx = self.function_converter.get_qbx(function_or_space, self._bdy_id)
        self.bound_op = bind((qbx, self.target), op)

    def __call__(self, queue, result_function=None, **kwargs):
        """
            Evaluates the operator for the given function.
            Any dof that is not a target point is set to 0.

            :arg queue: a :mod:`pyopencl` queue to use (usually
                made from the cl_ctx passed to this object
                during construction)
            :arg result_function: A function on the function space
                with non-target dofs already set to 0. If not passed in,
                one is constructed. This function will be modified
                and returned.
            :arg out_function_space: TODO
            :arg **kwargs: Arguments to pass to op. All :mod:`firedrake`
                :class:`Functions` are converted to pytential
        """
        new_kwargs = {}
        for key in kwargs:
            if isinstance(kwargs[key], Function):
                # Convert function to array with pytential ordering
                pyt_fntn = self.function_converter.convert(
                    queue, kwargs[key], bdy_id=self._bdy_id, put_on_array=True)

                new_kwargs[key] = pyt_fntn
            else:
                new_kwargs[key] = kwargs[key]

        # Perform operation and take result off queue
        result = self.bound_op(queue, **new_kwargs)
        result = result.get(queue=queue)

        # Create firedrake function
        if result_function is None:
            result_function = Function(self._out_fspace)
            result_function.dat.data[:] = 0.0

        if self.target_indices is not None:
            result_function.dat.data[self.target_indices] = result
        else:
            assert result_function is not None
            converter = self.function_converter.get_converter(result_function)
            result_function.dat.data[:] = converter.convert(
                queue, result, firedrake_to_meshmode=False)[:]

        return result_function


def fd_bind(converter, op, source=None, target=None):
    """
        :arg op: The operation
        :arg sources: either
            - A FunctionSpace, which will be the source
            - A pair (FunctionSpace, bdy_id) which will be the source
              (where bdy_id is the boundary which will be the source,
               *None* for the whole mesh)

        :arg targets: either
            - A FunctionSpace, which will be the target
            - A pair (FunctionSpace, bdy_id) which will be the target
              (where bdy_id is the boundary which will be the target,
               *None* for the whole mesh)
    """
    if isinstance(source, WithGeometry):
        source = (source, None)
    if isinstance(target, WithGeometry):
        target = (target, None)

    op_conn = OpConnection(converter, op, source[0], target[0],
                           targets=target[1],
                           source_bdy_id=source[1])
    return op_conn