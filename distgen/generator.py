from __future__ import print_function

import os, sys
import imp
import jinja2
from err import fatal
from distgen.pathmanager import PathManager
from distgen.config import load_config, merge_yaml
from distgen.project import AbstractProject
from distgen.commands import Commands


class Generator(object):
    project = None

    pm_cfg = None
    pm_tpl = None
    pm_spc = None


    def __init__(self):
        self.pm_cfg = PathManager(
            # TODO: Is there better way to reuse configured directories
            # from setup.py in python?
            ["/usr/share/distgen/distconf"],
            envvar="DG_DISTCONFDIR"
        )

        self.pm_tpl = PathManager(
            ['/usr/share/distgen/templates'],
            envvar="DG_TPLDIR"
        )

        self.pm_spc = PathManager([])


    def load_project(self, project):
        self.project = self._load_project_from_dir(project)
        if not self.project:
            self.project = AbstractProject()
        self.project.directory = project

        def absolute_load(name):
            """
            In our templating system, we care about filenames specified by
            absolute path, which is not truth for default FileSystemLoader.
            """
            if name.startswith('/'):
                try:
                    with file(name) as f:
                        return f.read().decode('utf-8')
                except:
                    pass
            raise jinja2.TemplateNotFound()

        loader = jinja2.ChoiceLoader([
            jinja2.FileSystemLoader(self.pm_tpl.get_path()),
            jinja2.FunctionLoader(absolute_load),
        ])

        self.project.tplgen = jinja2.Environment(
            loader=loader,
            keep_trailing_newline=True,
        )

        self.project.abstract_initialize()


    @staticmethod
    def _load_python_file(filename):
        """ load compiled python source """
        mod_name, file_ext = os.path.splitext(os.path.split(filename)[-1])
        if file_ext.lower() == '.py':
            py_mod = imp.load_source(mod_name, filename)
        elif file_ext.lower() == '.pyc':
            py_mod = imp.load_compiled(mod_name, filename)

        return py_mod


    def _load_obj_from_file(self, filename, objname):
        py_mod = self._load_python_file(filename)

        if hasattr(py_mod, objname):
            return getattr(py_mod, objname)
        else:
            return None


    def _load_obj_from_projdir(self, projectdir, objname):
        """ given project directory, load possibly existing project.py """
        project_file = projectdir + "/project.py"

        if os.path.isfile(project_file):
            return self._load_obj_from_file(project_file, objname)
        else:
            return None


    def _load_project_from_dir(self, projectdir):
        """ given project directory, load possibly existing project.py """
        projclass = self._load_obj_from_projdir(projectdir, "Project")
        if not projclass:
            return None
        return projclass()


    def load_config_from_project(self, directory):
        """
        read the project.py file for macros
        """
        config = self._load_obj_from_projdir(directory, 'config')
        if config:
            return config
        return {}


    @staticmethod
    def vars_fixed_point(config):
        """ substitute variables in paths """

        keys = config.keys()

        something_changed = True
        while something_changed:
            something_changed = False

            for i in keys:
                for j in keys:
                    if j == i:
                        continue
                    replaced = config[i].replace("$" + j, config[j])
                    if replaced != config[i]:
                        something_changed = True
                        config[i] = replaced


    def vars_fill_variables(self, config, sysconfig=None):
        if not 'macros' in config:
            return

        macros = config['macros']

        additional_macros = {}
        if sysconfig and 'macros' in sysconfig:
            additional_macros = sysconfig['macros']

        merged = merge_yaml(additional_macros, macros)
        if 'name' in config:
            merged['name'] = config['name']
        else:
            merged['name'] = 'unknown-pkg'
        self.vars_fixed_point(merged)

        config['macros'] = {x: merged[x] for x in macros.keys()}


    def render(self, specfiles, template, config, cmd_cfg,
               output=sys.stdout, confdirs=None, explicit_macros=None):
        """ render single template """
        config_path = [self.project.directory] + self.pm_cfg.get_path()
        sysconfig = load_config(config_path, config)

        if not confdirs:
            confdirs = []
        for i in confdirs + [self.project.directory]:
            additional_vars = self.load_config_from_project(i)
            self.vars_fill_variables(additional_vars, sysconfig)
            # filter only interresting variables
            interresting_parts = ['macros']
            additional_vars = {x: additional_vars[x] \
                    for x in interresting_parts if x in additional_vars}
            sysconfig = merge_yaml(sysconfig, additional_vars)

        self.project.abstract_setup_vars(sysconfig)

        init_data = self.project.inst_init(specfiles, template, sysconfig)

        projcfg = self.load_config_from_project(self.project.directory)
        if projcfg and 'name' in projcfg:
            sysconfig['name'] = projcfg['name']
        self.vars_fill_variables(sysconfig)

        explicit_macros = {'macros': explicit_macros}
        self.vars_fill_variables(explicit_macros, sysconfig)
        sysconfig = merge_yaml(sysconfig, explicit_macros)

        # NOTE: This is soo ugly, sorry for that, in future we need to modify
        # PyYAML to let us specify callbacks, somehow.  But for now, import
        # yaml right here to be able to add the constructors/representers
        # "locally".
        import yaml

        def _eval_node(loader, node):
            return str(eval(str(loader.construct_scalar(node)), {
                'init': init_data,
                'config': sysconfig,
                'macros': sysconfig['macros'],
            }))

        yaml.add_constructor(u'!eval', _eval_node)

        spec = {}
        for specfile in specfiles:
            specfd = self.pm_spc.open_file(
                specfile,
                [self.project.directory],
                fail=True,
            )
            if not specfd:
                fatal("Spec file {0} not found".format(specfile))

            try:
                specdata = yaml.load(specfd)
                spec = merge_yaml(spec, specdata)
            except yaml.YAMLError, exc:
                fatal("Error in spec file: {0}".format(exc))

        self.project.inst_finish(specfile, template, sysconfig, spec)

        try:
            tpl = self.project.tplgen.get_template(template)
        except jinja2.exceptions.TemplateNotFound as err:
            fatal("Can not find template {0}".format(err))

        output.write(tpl.render(
            config=sysconfig,
            macros=sysconfig["macros"],
            m=sysconfig["macros"],
            container={'name': 'docker'},
            spec=spec,
            project=self.project,
            commands=Commands(cmd_cfg, sysconfig),
            env=os.environ,
        ))
