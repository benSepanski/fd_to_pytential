from os.path import abspath, dirname, join
import pyopencl as cl
from pyopencl.tools import (  # noqa
    pytest_generate_tests_for_pyopencl as pytest_generate_tests)

# Need up here for WSL
cl_ctx = cl.create_some_context()
queue = cl.CommandQueue(cl_ctx)

import pytest

# This has been my convention since both firedrake and pytential
# have some similar names. This makes defining bilinear forms really
# unpleasant.
import firedrake as fd
from sumpy.kernel import LaplaceKernel
from pytential import sym

# The user should only need to interact with firedrake_to_pytential.op
import fd2mm

cwd = abspath(dirname(__file__))
mesh2d = fd.Mesh(join(cwd, 'meshes', 'circle.msh'))
mesh3d = fd.Mesh(join(cwd, 'meshes', 'ball.msh'))


@pytest.mark.parametrize('family', ['DG', 'CG'])
@pytest.mark.parametrize('degree', [1, 3])
@pytest.mark.parametrize('ambient_dim', [2, 3])
def test_greens_formula(degree, family, ambient_dim):

    fine_order = 4 * degree
    # Parameter to tune accuracy of pytential
    fmm_order = 5
    # This should be (order of convergence = qbx_order + 1)
    qbx_order = degree
    with_refinement = True

    qbx_kwargs = {'fine_order': fine_order,
                  'fmm_order': fmm_order,
                  'qbx_order': qbx_order}

    if ambient_dim == 2:
        mesh = mesh2d
        r"""
        ..math:

            \ln(\sqrt{(x+1)^2 + (y+1)^2})

        i.e. a shift of the fundamental solution
        """
        x, y = fd.SpatialCoordinate(mesh)
        expr = fd.ln(fd.sqrt((x + 2)**2 + (y + 2)**2))
    elif ambient_dim == 3:
        mesh = mesh3d
        x, y, z = fd.SpatialCoordinate(mesh)
        r"""
        ..math:

            \f{1}{4\pi \sqrt{(x-2)^2 + (y-2)^2 + (z-2)^2)}}

        i.e. a shift of the fundamental solution
        """
        expr = fd.Constant(1 / 4 / fd.pi) * 1 / fd.sqrt(
            (x - 2)**2 + (y - 2)**2 + (z-2)**2)
    else:
        raise ValueError("Ambient dimension must be 2 or 3, not %s" % ambient_dim)

    # Let's compute some layer potentials!
    V = fd.FunctionSpace(mesh, family, degree)
    Vdim = fd.VectorFunctionSpace(mesh, family, degree)

    mesh_analog = fd2mm.MeshAnalog(mesh)
    fspace_analog = fd2mm.FunctionSpaceAnalog(cl_ctx, mesh_analog, V)

    true_sol = fd.Function(V).interpolate(expr)
    grad_true_sol = fd.Function(Vdim).interpolate(fd.grad(expr))

    # Let's create an operator which plugs in f, \partial_n f
    # to Green's formula

    sigma = sym.make_sym_vector("sigma", ambient_dim)
    op = -(sym.D(LaplaceKernel(ambient_dim),
              sym.var("u"),
              qbx_forced_limit=None)
        - sym.S(LaplaceKernel(ambient_dim),
                sym.n_dot(sigma),
                qbx_forced_limit=None))

    from meshmode.mesh import BTAG_ALL
    outer_bdy_id = BTAG_ALL

    # Think of this like :mod:`pytential`'s :function:`bind`
    pyt_op = fd2mm.fd_bind(cl_ctx, fspace_analog, op, source=(V, outer_bdy_id),
                           target=V, qbx_kwargs=qbx_kwargs,
                           with_refinement=with_refinement)

    # Compute the operation and store in result
    result = fd.Function(V)

    pyt_op(queue, u=true_sol, sigma=grad_true_sol, result_function=result)

    # Compare with f
    fnorm = fd.sqrt(fd.assemble(fd.inner(true_sol, true_sol) * fd.dx))
    l2_err = fd.sqrt(fd.assemble(fd.inner(true_sol-result, true_sol-result) * fd.dx))
    rel_l2_err = l2_err / fnorm

    # TODO: Make this more strict
    assert rel_l2_err < 0.09
