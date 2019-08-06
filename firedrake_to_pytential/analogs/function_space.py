"""
    Used to raise user warnings
"""

import numpy as np
from numpy import linalg as la

from firedrake.functionspaceimpl import WithGeometry
from firedrake_to_pytential.analogs import Analog
from firedrake_to_pytential.analogs.cell import SimplexCellAnalog
from firedrake_to_pytential.analogs.finat_element import FinatElementAnalog
from firedrake_to_pytential.analogs.mesh import MeshAnalog, MeshAnalogNearBdy, \
    MeshAnalogWithBdy, MeshAnalogOnBdy


class FunctionSpaceAnalog(Analog):
    """
        NOTE : This is a special case of an Analog, because
               firedrake has more information than we need
               in a FunctionSpace. In particular,
               :function:`is_analog`
               is overwritten, as it is not what you normally
               would expect.
    """
    def __init__(self, function_space=None,
                 cell_analog=None, finat_element_analog=None, mesh_analog=None,
                 near_bdy=None, on_bdy=None):
        """
            :arg function_space: Either a :mod:`firedrake` function space or *None*.
                                 One should note that this function space is NOT
                                 stored in the object. This is so that different
                                 function spaces (e.g. a function space and a vector
                                 function space of the same degree on the same mesh)
                                 can share an Analog (see the class documentation)

            :arg mesh_analog:, :arg:`finat_element_analog`, and :arg:`cell_analog`
            are required if :arg:`function_space` is *None*.
            If the function space is known a priori, these are only passed in to
            avoid duplication of effort (e.g. if you are making multiple
            :class:`FunctionSpaceAnalog` objects on the same mesh, there's no
            reason for them both to construct :class:`MeshAnalogs`of that mesh).

            At least one of the following arguments must be *None*. If
            one of them is not *None*, then an appropriate subclass
            of :class:`MeshAnalogWithBdy` is used if available.

            :arg near_bdy: Same as for :class:`MeshAnalogNearBdy`
            :arg on_bdy: Same as for :class:`MeshAnalogOnBdy`
        """
        # TODO: Add an open init() function to compute things, rather
        #       than calling meshmode_mesh()

        # Check near_bdy and on_bdy
        assert near_bdy is None or on_bdy is None
        # If one is not *None*, store it in bdy_id
        bdy_id = on_bdy
        if near_bdy is not None:
            bdy_id = near_bdy

        # Construct analogs if necessary
        if function_space is not None:
            if cell_analog is None:
                cell_analog = SimplexCellAnalog(function_space.finat_element.cell)

            if finat_element_analog is None:
                finat_element_analog = FinatElementAnalog(
                    function_space.finat_element, cell_analog=cell_analog)

            if mesh_analog is None:
                if near_bdy is not None:
                    mesh_analog = MeshAnalogNearBdy(function_space.mesh(),
                                                    bdy_id)
                elif on_bdy is not None:
                    mesh_analog = MeshAnalogOnBdy(function_space.mesh(),
                                                  bdy_id)
                else:
                    mesh_analog = MeshAnalog(function_space.mesh())

        # Make sure the analogs are of the appropriate types
        assert isinstance(cell_analog, SimplexCellAnalog)
        assert isinstance(finat_element_analog, FinatElementAnalog)
        assert isinstance(mesh_analog, MeshAnalog)

        # Make sure the analogs are compatible

        if not cell_analog.is_analog(finat_element_analog.analog().cell):
            raise ValueError("Finat element analog and cell analog must refer"
                             " to the same cell")
        if function_space is not None:
            assert cell_analog.is_analog(function_space.finat_element.cell)
            assert finat_element_analog.is_analog(function_space.finat_element)
            assert mesh_analog.is_analog(function_space.mesh(), bdy_id=bdy_id)

        if bdy_id is None:
            # can't convert whole mesh if mesh analog only has bdy
            assert not isinstance(mesh_analog, MeshAnalogWithBdy)
        else:
            # otherwise make sure converting whole mesh, or at least
            # portion with given boundary
            assert not isinstance(mesh_analog, MeshAnalogWithBdy) or \
                mesh_analog.contains_bdy(bdy_id)

        # Initialize as Analog
        super(FunctionSpaceAnalog, self).__init__(
            (cell_analog.analog(), finat_element_analog.analog(),
             mesh_analog.analog()))

        self._nodes = None
        self._meshmode_mesh = None
        self._fd_to_mesh_reordering = None
        self._mesh_to_fd_reordering = None

        self._mesh_analog = mesh_analog
        self._cell_analog = cell_analog
        self._finat_element_analog = finat_element_analog

        # If we weren't given a function space, we'll compute these later
        self._cell_node_list = None
        self._num_fdnodes = None

        # If we were given a function space, no need to compute them again later!
        if function_space is not None:
            self._compute_fd_cell_nodes(function_space.cell_node_list)

    def is_analog(self, obj, **kwargs):
        """
            :kwarg bdy_id: As in construction of a :class:`MeshAnalogNearBdy`
                             defaults to *None*.

            Return whether or not this object is an analog for the
            given object and bdy_id (*None* represents no bdy_id)
        """
        bdy_id = kwargs.get('bdy_id', None)

        # object must be a function space with geometry
        if not isinstance(obj, WithGeometry):
            return False

        mesh = obj.mesh()
        finat_element = obj.finat_element
        cell = finat_element.cell

        # {{{ Make sure each of the above is an analog of the
        #     appropriate type, if not return *False*

        if not self._mesh_analog.is_analog(mesh, bdy_id=bdy_id):
            return False

        if not self._finat_element_analog.is_analog(finat_element):
            return False

        if not self._cell_analog.is_analog(cell):
            return False

        # }}}

        return True  # if made it here, obj is an analog

    def mesh_analog_type(self):
        """
            Returns the type of this object's mesh analog
        """
        return type(self._mesh_analog)

    def get_finat_element_analog(self):
        # FIXME: Memoize this?

        finat_element_analog = self._finat_element_analog
        # {{{ Need to get new finat element if converting only on bdy

        if isinstance(self._mesh_analog, MeshAnalogOnBdy):
            # Make a cell for one of the cell's faces
            cell = self._cell_analog.analog()
            dim = cell.get_dimension()
            sub_elt = cell.construct_subelement(dim - 1)

            # Construct a new finat element
            degree = finat_element_analog.analog().degree
            finat_element = type(finat_element_analog.analog())(sub_elt, degree)

            # make a new analog
            finat_element_analog = FinatElementAnalog(finat_element)

        # }}}

        return finat_element_analog

    def meshmode_mesh(self):
        if self._meshmode_mesh is None:

            vertex_indices = self._mesh_analog.vertex_indices()
            vertices = self._mesh_analog.vertices()

            # {{{ Compute nodes

            ambient_dim = self._mesh_analog.analog().geometric_dimension()
            nelements = vertex_indices.shape[0]

            finat_element_analog = self.get_finat_element_analog()
            bary_unit_nodes = finat_element_analog.barycentric_unit_nodes()
            nunit_nodes = bary_unit_nodes.shape[1]

            self._nodes = np.zeros((ambient_dim, nelements, nunit_nodes))

            for i, indices in enumerate(vertex_indices):
                elt_coords = np.zeros((ambient_dim, len(indices)))
                for j in range(elt_coords.shape[1]):
                    elt_coords[:, j] = vertices[:, indices[j]]

                # NOTE : Here, we are in effect 'creating' nodes for CG spaces,
                #        since come nodes that were shared along boundaries are now
                #        treated as independent
                #
                #        In particular, this node numbering may be different
                #        than firedrake's!
                #
                # This also relies on the mapping being affine.
                self._nodes[:, i, :] = np.matmul(elt_coords, bary_unit_nodes)[:, :]

            # }}}

            # {{{ Construct mesh and store reordered nodes

            from meshmode.mesh import SimplexElementGroup
            # Nb: topological_dimension() is a method from the firedrake mesh
            group = SimplexElementGroup(
                finat_element_analog.analog().degree,
                vertex_indices,
                self._nodes,
                dim=self._mesh_analog.topological_dimension(),
                unit_nodes=finat_element_analog.unit_nodes())

            from meshmode.mesh.processing import flip_simplex_element_group
            group = flip_simplex_element_group(vertices, group,
                                               self._mesh_analog.orientations() < 0)

            from meshmode.mesh import Mesh
            self._meshmode_mesh = Mesh(
                vertices,
                [group],
                boundary_tags=self._mesh_analog.bdy_tags(),
                facial_adjacency_groups=self._mesh_analog.facial_adjacency_groups())
            # }}}

            """
            from meshmode.mesh.visualization import draw_2d_mesh
            import matplotlib.pyplot as plt
            draw_2d_mesh(self._meshmode_mesh, draw_vertex_numbers=False, draw_element_numbers=False)
            plt.xlim(left=-4, right=4)
            plt.ylim(bottom=-4, top=4)
            plt.show()
            """

        return self._meshmode_mesh

    def num_meshmode_nodes(self):
        self.meshmode_mesh()  # Compute nodes
        # nelements * nunit_nodes
        return self._nodes.shape[1] * self._nodes.shape[2]

    def _compute_fd_cell_nodes(self, cell_node_list=None):
        # {{{ Construct firedrake cell node list if not already constructed
        if self._cell_node_list is None:
            if cell_node_list is None:
                # This is ripped out of some fd code to make cell node lists
                entity_dofs = self._finat_element_analog.analog().entity_dofs()
                mesh = self._mesh_analog.analog()
                nodes_per_entity = tuple(mesh.make_dofs_per_plex_entity(entity_dofs))

                # TODO: Put a warning or something
                # FIXME : Allow for real tensor products
                from firedrake.functionspacedata import get_global_numbering
                global_numbering = get_global_numbering(mesh,
                                                        (nodes_per_entity, False))
                self._cell_node_list = mesh.make_cell_node_list(global_numbering,
                                                                entity_dofs, None)
            else:
                self._cell_node_list = cell_node_list

            self._num_fdnodes = np.max(self._cell_node_list) + 1

            # Convert to only cells near bdy if only using cells there
            if isinstance(self._mesh_analog, MeshAnalogNearBdy):
                self._cell_node_list = self._cell_node_list[
                    self._mesh_analog.cell_id_to_fd_cell_id()]

            # Convert to facets on bdy if using those as cells
            elif isinstance(self._mesh_analog, MeshAnalogOnBdy):
                # FIXME: This only works for degree 1
                if self._finat_element_analog.analog().degree != 1:
                    raise ValueError("Currently can only do OnBdy for degree 1")

                self._cell_node_list = self._mesh_analog.vertex_indices()

        # }}}

    def num_firedrake_nodes(self):
        """
            Return the number of firedrake nodes
        """
        self._compute_fd_cell_nodes()
        return self._num_fdnodes

    def firedrake_cell_node_list(self):
        """
            Return the firedrake cell node list
        """
        self._compute_fd_cell_nodes()
        return self._cell_node_list

    def _reordering_array(self, firedrake_to_meshmode):
        """
        Returns a *np.array* that can reorder the data by composition,
        see :function:`reorder_nodes` below
        """
        # See if need to compute array
        order = None
        if (firedrake_to_meshmode and self._fd_to_mesh_reordering is None) or \
                (not firedrake_to_meshmode and self._mesh_to_fd_reordering is None):
            if firedrake_to_meshmode:
                order = np.arange(self.num_firedrake_nodes())
            else:
                order = np.arange(self.num_meshmode_nodes())

        # Compute permutation if not already done
        if order is not None:
            # reorder nodes (Code adapted from
            # meshmode.mesh.processing.flip_simplex_element_group)

            # {{{ get flip mat and obtain
            #     function data in form [nelements][nunit_nodes]

            # ( round to int bc applying on integers)
            finat_element_analog = self.get_finat_element_analog()
            flip_mat = np.rint(finat_element_analog.flip_matrix())
            if not firedrake_to_meshmode:
                flip_mat = flip_mat.T

            # flipping twice should be identity
            assert la.norm(
                np.dot(flip_mat, flip_mat)
                - np.eye(len(flip_mat))) < 1e-13

            # Put into cell-node list if firedrake-to meshmode (so can apply
            # flip-mat)
            if firedrake_to_meshmode:
                new_order = order[self.firedrake_cell_node_list()]
            # else just need to reshape new_order so that can apply flip-mat
            else:
                nunit_nodes = finat_element_analog.unit_nodes().shape[1]
                new_order = order.reshape(
                    (order.shape[0]//nunit_nodes, nunit_nodes) + order.shape[1:])

            # }}}

            # {{{ flip nodes that need to be flipped, note that this point we act
            #     like we are in a DG space

            orient = self._mesh_analog.orientations()
            # if a vector function space, new_order array is shaped differently
            if len(order.shape) > 1:
                new_order[orient < 0] = np.einsum(
                    "ij,ejk->eik",
                    flip_mat, new_order[orient < 0])
                # Reshape to [nodes][vector dims]
                new_order = new_order.reshape(
                    new_order.shape[0] * new_order.shape[1], new_order.shape[2])
                # pytential wants [vector dims][nodes] not [nodes][vector dims]
                new_order = new_order.T.copy()
            else:
                new_order[orient < 0] = np.einsum(
                    "ij,ej->ei",
                    flip_mat, new_order[orient < 0])
                # convert from [element][unit_nodes] to
                # global node number
                new_order = new_order.flatten()

            # Resize new_order if going meshmode->firedrake and meshmode
            # has duplicate nodes (e.g if used a CG fspace)
            if not firedrake_to_meshmode and \
                    self.num_firedrake_nodes() != self.num_meshmode_nodes():
                newnew_order = np.zeros(self.num_firedrake_nodes(), dtype=np.int32)
                pyt_ndx = 0
                for nodes in self.firedrake_cell_node_list():
                    for fd_index in nodes:
                        newnew_order[fd_index] = new_order[pyt_ndx]
                        pyt_ndx += 1

                new_order = newnew_order

            # Go ahead and free memory if we don't expect to use it again
            if self._fd_to_mesh_reordering is not None and (
                    self.num_firedrake_nodes() == self.num_meshmode_nodes()
                    or not firedrake_to_meshmode):
                del self._cell_node_list
                self._cell_node_list = None

            # }}}

            if firedrake_to_meshmode:
                self._fd_to_mesh_reordering = new_order
            else:
                self._mesh_to_fd_reordering = new_order

        # Return the appropriate array
        arr = None
        if firedrake_to_meshmode:
            arr = self._fd_to_mesh_reordering
        else:
            arr = self._mesh_to_fd_reordering

        return arr

    def reorder_nodes(self, nodes, firedrake_to_meshmode=True):
        """
        :arg nodes: An array representing function values at each of the
                    dofs
        :arg firedrake_to_meshmode: *True* iff firedrake->meshmode, *False*
            if reordering meshmode->firedrake
        """
        reordered_nodes = nodes[self._reordering_array(firedrake_to_meshmode)]
        # handle vector spaces
        if len(nodes.shape) > 1:
            reordered_nodes = reordered_nodes.T.copy()

        return reordered_nodes