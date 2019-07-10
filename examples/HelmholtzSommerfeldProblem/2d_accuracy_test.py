import os
import matplotlib.pyplot as plt
import pyopencl as cl

cl_ctx = cl.create_some_context()
queue = cl.CommandQueue(cl_ctx)

# For WSL, all firedrake must be imported after pyopencl
import firedrake as fd
from hankel_function import hankel_function
from methods.run_method import run_method

# Trial settings
mesh_file_dir = "circle_in_square/"  # NEED a forward slash at end
kappa_list = range(4, 9)
degree_list = [1]

method_list = ['nonlocal_integral_eq', 'pml', 'nonlocal_integral_eq']
method_to_kwargs = {
    'transmission': {},
    'pml': {
        'pml_type': 'bdy_integral'
    },
    'nonlocal_integral_eq': {
        'cl_ctx': cl_ctx,
        'queue': queue,
        'with_refinement': True,
        'epsilon': 0.01,
        'print_fmm_order': True}
    }

# Use cache if have it?
use_cache = True

# Visualize solutions?
visualize = False

keys = ('h', 'degree', 'kappa', 'method')

cache_file_name = "2d_accuracy_test.csv"
try:
    in_file = open(cache_file_name)
    lines = [tuple(line.replace(',', ' ')) for line in in_file]
    in_file.close()

    file_keys, lines = lines[0], lines[1:]
    assert set(file_keys) >= keys

    in_file.close()
except (OSError, IOError):
    cache = {}

# Hankel approximation cutoff
hankel_cutoff = 25

inner_bdy_id = 1
outer_bdy_id = 2
inner_region = 3
pml_x_region = 4
pml_y_region = 5
pml_xy_region = 6

pml_x_min = 2
pml_x_max = 3
pml_y_min = 2
pml_y_max = 3

# Set kwargs that don't expect user to change
# (some of these are for just pml, but we don't
#  expect the user to want to change them
global_kwargs = {'scatterer_bdy_id': inner_bdy_id,
                 'outer_bdy_id': outer_bdy_id,
                 'inner_region': inner_region,
                 'pml_x_region': pml_x_region,
                 'pml_y_region': pml_y_region,
                 'pml_xy_region': pml_xy_region,
                 'pml_x_min': pml_x_min,
                 'pml_x_max': pml_x_max,
                 'pml_y_min': pml_y_min,
                 'pml_y_max': pml_y_max,
                 }

# Go ahead and make the file directory accurate
mesh_file_dir = 'meshes/' + mesh_file_dir

# Ready kwargs by adding global kwargs to them
for mkey in method_to_kwargs:
    for gkey in global_kwargs:
        method_to_kwargs[mkey][gkey] = global_kwargs[gkey]

print("Reading Meshes...")
meshes = []
mesh_h_vals = []
for filename in os.listdir(mesh_file_dir):
    basename, ext = os.path.splitext(filename)  # remove ext
    if ext == '.msh':
        meshes.append(fd.Mesh(mesh_file_dir + basename + ext))

        hstr = basename[3:]
        hstr = hstr.replace("%", ".")
        h = float(hstr)
        mesh_h_vals.append(h)

meshes.sort(key=lambda x: x.coordinates.dat.data.shape[0])
mesh_h_vals.sort(reverse=True)
print("Meshes Read in.")


def get_key(setup_info):
    sorted_keys = sorted(setup_info.keys())
    key = tuple([setup_info[key] for key in sorted_keys])
    return key


def relative_error(true_sol, comp_sol):
    true_sol_norm = fd.sqrt(fd.assemble(
        fd.inner(true_sol, true_sol) * fd.dx(inner_region)
        ))
    l2_err = fd.sqrt(fd.assemble(
        fd.inner(true_sol - comp_sol, true_sol - comp_sol) * fd.dx(inner_region)
        ))
    return l2_err / true_sol_norm


# All the input parameters to a run
setup_info = {}
# Store error and functions
results = {}

iteration = 0
total_iter = len(meshes) * len(degree_list) * len(kappa_list) * len(method_list)

for mesh, mesh_h in zip(meshes, mesh_h_vals):
    setup_info['h'] = mesh_h
    x, y = fd.SpatialCoordinate(mesh)

    for degree in degree_list:
        setup_info['degree'] = degree

        for kappa in kappa_list:
            setup_info['kappa'] = kappa
            true_sol_expr = fd.Constant(1j / 4) \
                * hankel_function(kappa * fd.sqrt(x**2 + y**2),
                                  n=hankel_cutoff)

            trial = {'mesh': mesh,
                     'degree': degree,
                     'true_sol_expr': true_sol_expr}

            for method in method_list:
                setup_info['method'] = method

                key = get_key(setup_info)
                if not use_cache or key not in cache:
                    kwargs = method_to_kwargs[method]
                    true_sol, comp_sol = run_method(trial, method, kappa,
                                                    comp_sol_name=method
                                                    + " Computed Solution",
                                                    **kwargs)
                    rel_err = relative_error(true_sol, comp_sol)

                    ndofs = true_sol.dat.data.shape[0]

                    # Store method
                    results[key] = (rel_err, true_sol, comp_sol)
                    cache[key] = (rel_err, ndofs)
                    if visualize:
                        fd.plot(comp_sol)
                        fd.plot(true_sol)
                        plt.show()

                else:
                    rel_err, ndofs = cache[key]
                    results[key] = (rel_err, None, None)

                iteration += 1
                print('iter %s / %s' % (iteration, total_iter))
                print('h:', mesh_h)
                print("ndofs:", ndofs)
                print("kappa:", kappa)
                print("method:", method)
                print('degree:', degree)
                print("L^2 Relative Err: ", rel_err)
                print()

        # write to cache
        out_file = open(cache_file_name, 'wb')
        pickle.dump(cache, out_file)
        out_file.close()

# Add 'eta' back into setup info for plotting, if it was used
if eta_list:
    setup_info['eta'] = None


def make_plot(independent_var):
    if independent_var == 'eta':
        if len(method_list) > 1 or method_list[0] != 'nonlocal_integral_eq':
            raise ValueError('eta is only an independent variable for'
                             ' nonlocal_integral_eq')

    assert independent_var in setup_info.keys()
    key_to_key = {key: key for key in setup_info}
    key_entries = get_key(key_to_key)  # tuple of what each entry in the key is

    const_vars = set()
    const_vars_str = ""

    for i, entry in enumerate(key_entries):
        if entry == independent_var:
            continue

        entry_vals = set()
        for key in results:
            entry_vals.add(key[i])

        if len(entry_vals) == 1:
            const_vars.add(entry)
            val, = list(entry_vals)
            const_vars_str += str(entry) + "=" + str(val) + "; "

    # Map all but independent_var to [(x_1, y_1), ..., (x_n, y_n)]
    new_results = {}
    for key in results:
        new_setup_info = {key_entry: entry for
                          key_entry, entry in zip(key_entries, key)}

        ind_value = new_setup_info[independent_var]
        del new_setup_info[independent_var]
        for const_var in const_vars:
            del new_setup_info[const_var]

        new_key = get_key(new_setup_info)

        rel_err = results[key][0]
        new_results.setdefault(new_key, []).append((ind_value, rel_err))

    fig = plt.figure()
    ax = fig.add_subplot(111)
    ax.set_title("Relative Error vs. %s\n%s" % (independent_var, const_vars_str))
    for key in new_results:
        x, y = zip(*(new_results[key]))  # This actually unzips

        label = ""
        i = 0
        for entry in key_entries:
            if entry == independent_var or entry in const_vars:
                continue
            val = key[i]
            if isinstance(val, float):
                val = round(val, 4)
            elif isinstance(val, complex):
                val = round(val.real, 4) + round(val.imag, 4) * 1j
            label += str(entry) + "=" + str(val) + "; "
            i += 1

        ax.scatter(x, y, label=label)
        ax.set_xlabel(independent_var)
        ax.set_ylabel("Relative Error")

    if len(new_results) <= 10:
        ax.legend()


"""
    The below was for testing different values of eta
"""
"""

key_to_key = {key: key for key in setup_info}
key_entries = get_key(key_to_key)  # tuple of what each entry in the key is
eta_index = key_entries.index('eta')
kappa_index = key_entries.index('kappa')

xs, ys, color = {}, {}, {}
for key in results:
    x = key[eta_index].real
    y = key[eta_index].imag
    kappa = key[kappa_index]

    if kappa not in xs:
        xs[kappa] = []
        ys[kappa] = []
        color[kappa] = []

    intensity = results[key][0]

    xs[kappa].append(x)
    ys[kappa].append(y)
    color[kappa].append(intensity)

num_plot = -1
for kappa in xs:
    thetas = [math.atan2(y, x) for x, y in zip(xs[kappa], ys[kappa])]
    theta_to_result = dict(zip(thetas, color[kappa]))
    thetas.sort(key=theta_to_result.get)

    thetas = thetas[:num_plot]

    plt.scatter(thetas, [theta_to_result[t] for t in thetas], label='kappa=%s' % kappa)

plt.legend()
"""

#make_plot('kappa')
plt.show()