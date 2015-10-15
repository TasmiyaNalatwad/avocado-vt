import os
import time
import commands
import re
import logging
import glob
import threading
import shutil
import sys
import copy
import multiprocessing

import aexpect
from avocado.utils import process as avocado_process
from avocado.utils import crypto
from avocado.utils import path
from avocado.core import exceptions

from . import error_context
from . import qemu_monitor
from . import ppm_utils
from . import test_setup
from . import virt_vm
from . import utils_misc
from . import storage
from . import qemu_storage
from . import utils_libvirtd
from . import remote
from . import data_dir
from . import utils_net
from . import utils_disk
from . import nfs
from . import libvirt_vm

try:
    import PIL.Image
except ImportError:
    logging.warning('No python imaging library installed. PPM image '
                    'conversion to JPEG disabled. In order to enable it, '
                    'please install python-imaging or the equivalent for your '
                    'distro.')

_screendump_thread = None
_screendump_thread_termination_event = None

_vm_register_thread = None
_vm_register_thread_termination_event = None

kernel_modified = False
kernel_cmdline = None


def preprocess_image(test, params, image_name, vm_process_status=None):
    """
    Preprocess a single QEMU image according to the instructions in params.

    :param test: Autotest test object.
    :param params: A dict containing image preprocessing parameters.
    :param vm_process_status: This is needed in postprocess_image. Add it here
                              only for keep it work with process_images()
    :note: Currently this function just creates an image if requested.
    """
    base_dir = params.get("images_base_dir", data_dir.get_data_dir())

    if not storage.preprocess_image_backend(base_dir, params, image_name):
        logging.error("Backend can't be prepared correctly.")

    image_filename = storage.get_image_filename(params,
                                                base_dir)

    create_image = False
    if params.get("force_create_image") == "yes":
        create_image = True
    elif (params.get("create_image") == "yes" and not
          storage.file_exists(params, image_filename)):
        create_image = True

    if params.get("backup_image_before_testing", "no") == "yes":
        image = qemu_storage.QemuImg(params, base_dir, image_name)
        image.backup_image(params, base_dir, "backup", True, True)
    if create_image:
        if storage.file_exists(params, image_filename):
            # As rbd image can not be covered, so need remove it if we need
            # force create a new image.
            storage.file_remove(params, image_filename)
        image = qemu_storage.QemuImg(params, base_dir, image_name)
        logging.info("Create image on %s." % image.storage_type)
        image.create(params)


def preprocess_vm(test, params, env, name):
    """
    Preprocess a single VM object according to the instructions in params.
    Start the VM if requested and get a screendump.

    :param test: An Autotest test object.
    :param params: A dict containing VM preprocessing parameters.
    :param env: The environment (a dict-like object).
    :param name: The name of the VM object.
    """
    vm = env.get_vm(name)
    vm_type = params.get('vm_type')
    connect_uri = params.get('connect_uri')
    target = params.get('target')

    create_vm = False
    if not vm:
        create_vm = True
    elif vm_type == 'libvirt':
        connect_uri = libvirt_vm.normalize_connect_uri(connect_uri)
        if (not vm.connect_uri == connect_uri):
            create_vm = True
    else:
        pass
    if create_vm:
        vm = env.create_vm(vm_type, target, name, params, test.bindir)

    old_vm = copy.copy(vm)

    if vm_type == 'libvirt':
        install_test = ('unattended_install.import.import.default_install.'
                        'aio_native')
        remove_test = 'remove_guest.without_disk'
        if not vm.exists() and (params.get("type") != "unattended_install" and
                                params.get("type") != "svirt_install"):
            error_msg = "Test VM %s does not exist." % name
            if name == params.get("main_vm"):
                error_msg += (" Consider adding '%s' test as the first one "
                              "and '%s' test as last one to remove the "
                              "guest after testing" %
                              (install_test, remove_test))
                raise exceptions.TestError(error_msg)
            else:
                raise exceptions.TestNAError(error_msg)

    remove_vm = False
    if params.get("force_remove_vm") == "yes":
        remove_vm = True

    if remove_vm:
        vm.remove()

    start_vm = False
    update_virtnet = False
    gracefully_kill = params.get("kill_vm_gracefully") == "yes"

    if params.get("migration_mode"):
        start_vm = True
    elif params.get("start_vm") == "yes":
        # need to deal with libvirt VM differently than qemu
        if vm_type == 'libvirt' or vm_type == 'v2v':
            if not vm.is_alive():
                start_vm = True
        else:
            if not vm.is_alive():
                start_vm = True
            if params.get("check_vm_needs_restart", "yes") == "yes":
                if vm.needs_restart(name=name,
                                    params=params,
                                    basedir=test.bindir):
                    vm.devices = None
                    start_vm = True
                    old_vm.destroy(gracefully=gracefully_kill)
                    update_virtnet = True

    if start_vm:
        if vm_type == "libvirt" and params.get("type") != "unattended_install":
            vm.params = params
            vm.start()
        elif vm_type == "v2v":
            vm.params = params
            vm.start()
        else:
            if update_virtnet:
                vm.update_vm_id()
                vm.virtnet = utils_net.VirtNet(params, name, vm.instance)
            # Start the VM (or restart it if it's already up)
            if params.get("reuse_previous_config", "no") == "no":
                vm.create(name, params, test.bindir,
                          timeout=int(params.get("vm_create_timeout", 20)),
                          migration_mode=params.get("migration_mode"),
                          migration_fd=params.get("migration_fd"),
                          migration_exec_cmd=params.get("migration_exec_cmd_dst"))
            else:
                vm.create(timeout=int(params.get("vm_create_timeout", 20)),
                          migration_mode=params.get("migration_mode"),
                          migration_fd=params.get("migration_fd"),
                          migration_exec_cmd=params.get("migration_exec_cmd_dst"))
    elif not vm.is_alive():    # VM is dead and won't be started, update params
        vm.devices = None
        vm.params = params
    else:
        # Only work when parameter 'start_vm' is no and VM is alive
        if params.get("kill_vm_before_test") == "yes" and\
           params.get("start_vm") == "no":
            old_vm.destroy(gracefully=gracefully_kill)
        else:
            # VM is alive and we just need to open the serial console
            vm.create_serial_console()

    pause_vm = False

    if params.get("paused_after_start_vm") == "yes":
        pause_vm = True
        # Check the status of vm
        if (not vm.is_alive()) or (vm.is_paused()):
            pause_vm = False

    if pause_vm:
        vm.pause()

    if params.get("check_kernel_cmd_line_from_serial") == "yes":
        debug_msg = ""
        if vm.is_paused():
            debug_msg += "VM is paused."
        elif not vm.is_alive():
            debug_msg += "VM is not alive."
        elif vm.serial_console is None:
            debug_msg += "There is no serial console in VM."
        if debug_msg:
            debug_msg += " Skip the kernel command line check."
            logging.warn(debug_msg)
            return
        cmd_line = params.get("kernel_cmd_line_str", "Command line:")
        try:
            output = vm.serial_console.read_until_output_matches(cmd_line,
                                                                 timeout=60)

            kernel_cmd_line = re.findall("%s.*" % cmd_line, output[1])[0]
            kernel_options_exist = params.get("kernel_options_exist", "")
            kernel_options_not_exist = params.get("kernel_options_not_exist",
                                                  "")

            err_msg = ""
            for kernel_option in kernel_options_exist.split():
                if kernel_option not in kernel_cmd_line:
                    err_msg += "%s not in kernel command line" % kernel_option
                    err_msg += " as expect."
            for kernel_option in kernel_options_not_exist.split():
                if kernel_option in kernel_cmd_line:
                    err_msg += "%s exist in kernel command" % kernel_option
                    err_msg += " line."
            if err_msg:
                err_msg += " Kernel command line get from"
                err_msg += " serial output is %s" % kernel_cmd_line
                raise exceptions.TestError(err_msg)

            logging.info("Kernel command line get from serial port is"
                         " as expect")
        except Exception, err:
            logging.warn("Did not get the kernel command line from serial "
                         "port output. Skip the kernel command line check."
                         "Error is %s" % err)


def postprocess_image(test, params, image_name, vm_process_status=None):
    """
    Postprocess a single QEMU image according to the instructions in params.

    :param test: An Autotest test object.
    :param params: A dict containing image postprocessing parameters.
    :param vm_process_status: (optional) vm process status like running, dead
                              or None for no vm exist.
    """
    restored = False
    clone_master = params.get("clone_master", None)
    base_dir = data_dir.get_data_dir()
    image = qemu_storage.QemuImg(params, base_dir, image_name)

    check_image_flag = params.get("check_image") == "yes"
    if vm_process_status == "running" and check_image_flag:
        if params.get("skip_image_check_during_running") == "yes":
            logging.debug("Guest is still running, skip the image check.")
            check_image_flag = False
        else:
            image_info_output = image.info()
            image_info = {}
            if image_info_output is not None:
                for image_info_item in image_info_output.splitlines():
                    option = image_info_item.split(":")
                    if len(option) == 2:
                        image_info[option[0].strip()] = option[1].strip()
            else:
                logging.debug("Can not find matched image for selected guest "
                              "os, skip the image check.")
                check_image_flag = False
            if ("lazy refcounts" in image_info and
                    image_info["lazy refcounts"] == "true"):
                logging.debug("Should not check image while guest is alive"
                              " when the image is create with lazy refcounts."
                              " Skip the image check.")
                check_image_flag = False

    if check_image_flag:
        try:
            if clone_master is None:
                image.check_image(params, base_dir)
            elif clone_master == "yes":
                if image_name in params.get("master_images_clone").split():
                    image.check_image(params, base_dir)
            # Allow test to overwrite any pre-testing  automatic backup
            # with a new backup. i.e. assume pre-existing image/backup
            # would not be usable after this test succeeds. The best
            # example for this is when 'unattended_install' is run.
            if params.get("backup_image", "no") == "yes":
                image.backup_image(params, base_dir, "backup", True)
                restored = True
            elif params.get("restore_image", "no") == "yes":
                image.backup_image(params, base_dir, "restore", True)
                restored = True
        except Exception, e:
            if params.get("restore_image_on_check_error", "no") == "yes":
                image.backup_image(params, base_dir, "restore", True)
            if params.get("remove_image_on_check_error", "no") == "yes":
                cl_images = params.get("master_images_clone", "")
                if image_name in cl_images.split():
                    image.remove()
            if (params.get("skip_cluster_leak_warn") == "yes" and
                    "Leaked clusters" in e.message):
                logging.warn(e.message)
            else:
                raise e
    if (not restored and
            params.get("restore_image_after_testing", "no") == "yes"):
        image.backup_image(params, base_dir, "restore", True)
    if params.get("remove_image") == "yes":
        logging.info("Remove image on %s." % image.storage_type)
        if clone_master is None:
            image.remove()
        elif clone_master == "yes":
            if image_name in params.get("master_images_clone").split():
                image.remove()


def postprocess_vm(test, params, env, name):
    """
    Postprocess a single VM object according to the instructions in params.
    Kill the VM if requested and get a screendump.

    :param test: An Autotest test object.
    :param params: A dict containing VM postprocessing parameters.
    :param env: The environment (a dict-like object).
    :param name: The name of the VM object.
    """
    vm = env.get_vm(name)
    if not vm:
        return

    # Close all SSH sessions that might be active to this VM
    for s in vm.remote_sessions[:]:
        try:
            s.close()
            vm.remote_sessions.remove(s)
        except Exception:
            pass

    if params.get("kill_vm") == "yes":
        kill_vm_timeout = float(params.get("kill_vm_timeout", 0))
        if kill_vm_timeout:
            utils_misc.wait_for(vm.is_dead, kill_vm_timeout, 0, 1)
        vm.destroy(gracefully=params.get("kill_vm_gracefully") == "yes")


def process_command(test, params, env, command, command_timeout,
                    command_noncritical):
    """
    Pre- or post- custom commands to be executed before/after a test is run

    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param env: The environment (a dict-like object).
    :param command: Command to be run.
    :param command_timeout: Timeout for command execution.
    :param command_noncritical: If True test will not fail if command fails.
    """
    # Export environment vars
    for k in params:
        os.putenv("KVM_TEST_%s" % k, str(params[k]))
    # Execute commands
    try:
        avocado_process.system("cd %s; %s" % (test.bindir, command), shell=True)
    except avocado_process.CmdError, e:
        if command_noncritical:
            logging.warn(e)
        else:
            raise


class _CreateImages(threading.Thread):

    """
    Thread which creates images. In case of failure it stores the exception
    in self.exc_info
    """

    def __init__(self, image_func, test, images, params, exit_event,
                 vm_process_status):
        threading.Thread.__init__(self)
        self.image_func = image_func
        self.test = test
        self.images = images
        self.params = params
        self.exit_event = exit_event
        self.exc_info = None
        self.vm_process_status = vm_process_status

    def run(self):
        try:
            _process_images_serial(self.image_func, self.test, self.images,
                                   self.params, self.exit_event,
                                   self.vm_process_status)
        except Exception:
            self.exc_info = sys.exc_info()
            self.exit_event.set()


def process_images(image_func, test, params, vm_process_status=None):
    """
    Wrapper which chooses the best way to process images.

    :param image_func: Process function
    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param vm_process_status: (optional) vm process status like running, dead
                              or None for no vm exist.
    """
    images = params.objects("images")
    if len(images) > 20:    # Lets do it in parallel
        _process_images_parallel(image_func, test, params,
                                 vm_process_status=vm_process_status)
    else:
        _process_images_serial(image_func, test, images, params,
                               vm_process_status=vm_process_status)


def _process_images_serial(image_func, test, images, params, exit_event=None,
                           vm_process_status=None):
    """
    Original process_image function, which allows custom set of images
    :param image_func: Process function
    :param test: An Autotest test object.
    :param images: List of images (usually params.objects("images"))
    :param params: A dict containing all VM and image parameters.
    :param exit_event: (optional) exit event which interrupts the processing
    :param vm_process_status: (optional) vm process status like running, dead
                              or None for no vm exist.
    """
    for image_name in images:
        image_params = params.object_params(image_name)
        image_func(test, image_params, image_name, vm_process_status)
        if exit_event and exit_event.is_set():
            logging.error("Received exit_event, stop processing of images.")
            break


def _process_images_parallel(image_func, test, params, vm_process_status=None):
    """
    The same as _process_images but in parallel.
    :param image_func: Process function
    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param vm_process_status: (optional) vm process status like running, dead
                              or None for no vm exist.
    """
    images = params.objects("images")
    no_threads = min(len(images) / 5,
                     2 * multiprocessing.cpu_count())
    exit_event = threading.Event()
    threads = []
    for i in xrange(no_threads):
        imgs = images[i::no_threads]
        threads.append(_CreateImages(image_func, test, imgs, params,
                                     exit_event, vm_process_status))
        threads[-1].start()
    finished = False
    while not finished:
        finished = True
        for thread in threads:
            if thread.is_alive():
                finished = False
                time.sleep(0.5)
                break
    if exit_event.is_set():     # Failure in some thread
        logging.error("Image processing failed:")
        for thread in threads:
            if thread.exc_info:     # Throw the first failure
                raise thread.exc_info[1], None, thread.exc_info[2]
    del exit_event
    del threads[:]


def process(test, params, env, image_func, vm_func, vm_first=False):
    """
    Pre- or post-process VMs and images according to the instructions in params.
    Call image_func for each image listed in params and vm_func for each VM.

    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param env: The environment (a dict-like object).
    :param image_func: A function to call for each image.
    :param vm_func: A function to call for each VM.
    :param vm_first: Call vm_func first or not.
    """
    def _call_vm_func():
        for vm_name in params.objects("vms"):
            vm_params = params.object_params(vm_name)
            vm_func(test, vm_params, env, vm_name)

    def _call_image_func():
        if params.get("skip_image_processing") == "yes":
            return

        if params.objects("vms"):
            for vm_name in params.objects("vms"):
                vm_params = params.object_params(vm_name)
                vm = env.get_vm(vm_name)
                unpause_vm = False
                if vm is None or vm.is_dead():
                    vm_process_status = 'dead'
                else:
                    vm_process_status = 'running'
                if vm is not None and vm.is_alive() and not vm.is_paused():
                    vm.pause()
                    unpause_vm = True
                    vm_params['skip_cluster_leak_warn'] = "yes"
                try:
                    process_images(image_func, test, vm_params,
                                   vm_process_status)
                finally:
                    if unpause_vm:
                        vm.resume()
        else:
            process_images(image_func, test, params)

    if not vm_first:
        _call_image_func()

    _call_vm_func()

    if vm_first:
        _call_image_func()


@error_context.context_aware
def preprocess(test, params, env):
    """
    Preprocess all VMs and images according to the instructions in params.
    Also, collect some host information, such as the KVM version.

    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param env: The environment (a dict-like object).
    """
    error_context.context("preprocessing")
    # First, let's verify if this test does require root or not. If it
    # does and the test suite is running as a regular user, we shall just
    # throw a TestNAError exception, which will skip the test.
    if params.get('requires_root', 'no') == 'yes':
        utils_misc.verify_running_as_root()

    # throw a TestNAError exception if command requested by test is not
    # installed.
    if params.get("cmds_installed_host"):
        for cmd in params.get("cmds_installed_host").split():
            try:
                path.find_command(cmd)
            except path.CmdNotFoundError, msg:
                raise exceptions.TestNAError(msg.message)

    vm_type = params.get('vm_type')

    setup_pb = False
    ovs_pb = False
    for nic in params.get('nics', "").split():
        nic_params = params.object_params(nic)
        if nic_params.get('netdst') == 'private':
            setup_pb = True
            params_pb = nic_params
            params['netdst_%s' % nic] = nic_params.get("priv_brname", 'atbr0')
            if nic_params.get("priv_br_type") == "openvswitch":
                ovs_pb = True

    if setup_pb:
        if ovs_pb:
            brcfg = test_setup.PrivateOvsBridgeConfig(params_pb)
        else:
            brcfg = test_setup.PrivateBridgeConfig(params_pb)
        brcfg.setup()

    base_dir = data_dir.get_data_dir()
    if params.get("storage_type") == "iscsi":
        iscsidev = qemu_storage.Iscsidev(params, base_dir, "iscsi")
        params["image_name"] = iscsidev.setup()
        params["image_raw_device"] = "yes"

    if params.get("storage_type") == "lvm":
        lvmdev = qemu_storage.LVMdev(params, base_dir, "lvm")
        params["image_name"] = lvmdev.setup()
        params["image_raw_device"] = "yes"
        env.register_lvmdev("lvm_%s" % params["main_vm"], lvmdev)

    if params.get("storage_type") == "nfs":
        image_nfs = nfs.Nfs(params)
        image_nfs.setup()
        image_name_only = os.path.basename(params["image_name"])
        params['image_name'] = os.path.join(image_nfs.mount_dir,
                                            image_name_only)
        for image_name in params.objects("images"):
            name_tag = "image_name_%s" % image_name
            if params.get(name_tag):
                image_name_only = os.path.basename(params[name_tag])
                params[name_tag] = os.path.join(image_nfs.mount_dir,
                                                image_name_only)

    # Start tcpdump if it isn't already running
    # The fact it has to be started here is so that the test params
    # have to be honored.
    env.start_tcpdump(params)

    # Add migrate_vms to vms
    migrate_vms = params.objects("migrate_vms")
    if migrate_vms:
        vms = list(set(params.objects("vms") + migrate_vms))
        params["vms"] = ' '.join(vms)

    # Destroy and remove VMs that are no longer needed in the environment
    requested_vms = params.objects("vms")
    for key in env.keys():
        vm = env[key]
        if not isinstance(vm, virt_vm.BaseVM):
            continue
        if vm.name not in requested_vms:
            vm.destroy()
            del env[key]

    if (params.get("auto_cpu_model") == "yes" and
            vm_type == "qemu"):
        if not env.get("cpu_model"):
            env["cpu_model"] = utils_misc.get_qemu_best_cpu_model(params)
        params["cpu_model"] = env.get("cpu_model")

    kvm_ver_cmd = params.get("kvm_ver_cmd", "")

    if kvm_ver_cmd:
        try:
            cmd_result = avocado_process.run(kvm_ver_cmd)
            kvm_version = cmd_result.stdout.strip()
        except avocado_process.CmdError:
            kvm_version = "Unknown"
    else:
        # Get the KVM kernel module version and write it as a keyval
        if os.path.exists("/dev/kvm"):
            try:
                kvm_version = open("/sys/module/kvm/version").read().strip()
            except Exception:
                kvm_version = os.uname()[2]
        else:
            logging.warning("KVM module not loaded")
            kvm_version = "Unknown"

    logging.debug("KVM version: %s" % kvm_version)
    test.write_test_keyval({"kvm_version": kvm_version})

    # Get the KVM userspace version and write it as a keyval
    kvm_userspace_ver_cmd = params.get("kvm_userspace_ver_cmd", "")

    if kvm_userspace_ver_cmd:
        try:
            cmd_result = avocado_process.run(kvm_userspace_ver_cmd)
            kvm_userspace_version = cmd_result.stdout.strip()
        except avocado_process.CmdError:
            kvm_userspace_version = "Unknown"
    else:
        qemu_path = utils_misc.get_qemu_binary(params)
        version_line = commands.getoutput("%s -help | head -n 1" % qemu_path)
        matches = re.findall("[Vv]ersion .*?,", version_line)
        if matches:
            kvm_userspace_version = " ".join(matches[0].split()[1:]).strip(",")
        else:
            kvm_userspace_version = "Unknown"

    logging.debug("KVM userspace version: %s" % kvm_userspace_version)
    test.write_test_keyval({"kvm_userspace_version": kvm_userspace_version})

    libvirtd_inst = utils_libvirtd.Libvirtd()

    if params.get("setup_hugepages") == "yes":
        h = test_setup.HugePageConfig(params)
        suggest_mem = h.setup()
        if suggest_mem is not None:
            params['mem'] = suggest_mem
        if vm_type == "libvirt":
            libvirtd_inst.restart()

    if params.get("setup_thp") == "yes":
        thp = test_setup.TransparentHugePageConfig(test, params)
        thp.setup()

    if params.get("setup_ksm") == "yes":
        ksm = test_setup.KSMConfig(params, env)
        ksm.setup(env)

    if params.get("setup_egd") == "yes":
        egd = test_setup.EGDConfig(params, env)
        egd.setup()

    if vm_type == "libvirt":
        if params.get("setup_libvirt_polkit") == "yes":
            pol = test_setup.LibvirtPolkitConfig(params)
            try:
                pol.setup()
            except test_setup.PolkitWriteLibvirtdConfigError, e:
                logging.error(str(e))
            except test_setup.PolkitRulesSetupError, e:
                logging.error(str(e))
            except Exception, e:
                logging.error("Unexpected error: '%s'" % str(e))
            libvirtd_inst.restart()

    if vm_type == "libvirt":
        connect_uri = params.get("connect_uri")
        connect_uri = libvirt_vm.normalize_connect_uri(connect_uri)
        # Set the LIBVIRT_DEFAULT_URI to make virsh command
        # work on connect_uri as default behavior.
        os.environ['LIBVIRT_DEFAULT_URI'] = connect_uri

    # Execute any pre_commands
    if params.get("pre_command"):
        process_command(test, params, env, params.get("pre_command"),
                        int(params.get("pre_command_timeout", "600")),
                        params.get("pre_command_noncritical") == "yes")

    kernel_extra_params_add = params.get("kernel_extra_params_add", "")
    kernel_extra_params_remove = params.get("kernel_extra_params_remove", "")
    if params.get("disable_pci_msi"):
        disable_pci_msi = params.get("disable-pci_msi")
        if disable_pci_msi == "yes":
            if "pci=" in kernel_extra_params_add:
                kernel_extra_params_add = re.sub("pci=.*?\s+", "pci=nomsi ",
                                                 kernel_extra_params_add)
            else:
                kernel_extra_params_add += " pci=nomsi"
            params["ker_remove_similar_pci"] = "yes"
        else:
            kernel_extra_params_remove += " pci=nomsi"

    if kernel_extra_params_add or kernel_extra_params_remove:
        global kernel_cmdline, kernel_modified
        image_filename = storage.get_image_filename(params,
                                                    data_dir.get_data_dir())
        grub_file = params.get("grub_file", "/boot/grub2/grub.cfg")
        kernel_cfg_pos_reg = params.get("kernel_cfg_pos_reg",
                                        r".*vmlinuz-\d+.*")

        disk_obj = utils_disk.GuestFSModiDisk(image_filename)
        kernel_config_ori = disk_obj.read_file(grub_file)
        kernel_config = re.findall(kernel_cfg_pos_reg, kernel_config_ori)
        if not kernel_config:
            raise exceptions.TestError("Cannot find the kernel config, reg "
                                       "is %s" % kernel_cfg_pos_reg)
        kernel_config = kernel_config[0]
        kernel_cmdline = kernel_config

        kernel_need_modify = False
        kernel_config_set = kernel_config
        debug_msg = "Guest cmdline extra_params setting:"
        if kernel_extra_params_add:
            debug_msg += " added '%s'" % kernel_extra_params_add
            kernel_extra_params = kernel_extra_params_add.split()
            for kernel_extra_param in kernel_extra_params:
                param_tag = kernel_extra_param.split("=")[0]
                params_kernel = params.object_params(param_tag)
                rm_s = params_kernel.get("ker_remove_similar", "no") == "yes"
                kernel_config_set = utils_misc.add_ker_cmd(kernel_config_set,
                                                           kernel_extra_param,
                                                           rm_s)
        if kernel_extra_params_remove:
            debug_msg += " removed '%s'" % kernel_extra_params_remove
            kernel_extra_params = kernel_extra_params_remove.split()
            for kernel_extra_param in kernel_extra_params:
                kernel_config_set = utils_misc.rm_ker_cmd(kernel_config_set,
                                                          kernel_extra_param)

        if kernel_config_set.strip() != kernel_cmdline.strip():
            kernel_need_modify = True

        if kernel_need_modify:
            for vm in env.get_all_vms():
                if vm:
                    vm.destroy()
                    env.unregister_vm(vm.name)
            disk_obj.replace_image_file_content(grub_file, kernel_config,
                                                kernel_config_set)
            kernel_modified = True
        del disk_obj
        params["check_kernel_cmd_line_from_serial"] = "yes"
        if kernel_extra_params_add:
            params['kernel_options_exist'] = kernel_extra_params_add
        if kernel_extra_params_remove:
            params['kernel_options_not_exist'] = kernel_extra_params_remove
        logging.debug(debug_msg)

    # Clone master image from vms.
    base_dir = data_dir.get_data_dir()
    if params.get("master_images_clone"):
        for vm_name in params.get("vms").split():
            vm = env.get_vm(vm_name)
            if vm:
                vm.destroy()
                env.unregister_vm(vm_name)

            vm_params = params.object_params(vm_name)
            for image in vm_params.get("master_images_clone").split():
                image_obj = qemu_storage.QemuImg(params, base_dir, image)
                image_obj.clone_image(params, vm_name, image, base_dir)

    # Preprocess all VMs and images
    if params.get("not_preprocess", "no") == "no":
        process(test, params, env, preprocess_image, preprocess_vm)

    # Start the screendump thread
    if params.get("take_regular_screendumps") == "yes":
        global _screendump_thread, _screendump_thread_termination_event
        _screendump_thread_termination_event = threading.Event()
        _screendump_thread = threading.Thread(target=_take_screendumps,
                                              name='ScreenDump',
                                              args=(test, params, env))
        _screendump_thread.start()

    # Start the register query thread
    if params.get("store_vm_register") == "yes" and\
       params.get("vm_type") == "qemu":
        global _vm_register_thread, _vm_register_thread_termination_event
        _vm_register_thread_termination_event = threading.Event()
        _vm_register_thread = threading.Thread(target=_store_vm_register,
                                               name='VmRegister',
                                               args=(test, params, env))
        _vm_register_thread.start()

    return params


@error_context.context_aware
def postprocess(test, params, env):
    """
    Postprocess all VMs and images according to the instructions in params.

    :param test: An Autotest test object.
    :param params: Dict containing all VM and image parameters.
    :param env: The environment (a dict-like object).
    """
    error_context.context("postprocessing")
    err = ""

    # Postprocess all VMs and images
    try:
        process(test, params, env, postprocess_image, postprocess_vm,
                vm_first=True)
    except Exception, details:
        err += "\nPostprocess: %s" % str(details).replace('\\n', '\n  ')
        logging.error(details)

    # Terminate the screendump thread
    global _screendump_thread, _screendump_thread_termination_event
    if _screendump_thread is not None:
        _screendump_thread_termination_event.set()
        _screendump_thread.join(10)
        _screendump_thread = None

    # Encode an HTML 5 compatible video from the screenshots produced
    dir_rex = "(screendump\S*_[0-9]+_iter%s)" % test.iteration
    screendump_dir = re.findall(dir_rex, str(os.listdir(test.debugdir)))
    if screendump_dir:
        screendump_dir = os.path.join(test.debugdir, screendump_dir[0])
        if (params.get("encode_video_files", "yes") == "yes" and
                glob.glob("%s/*" % screendump_dir)):
            try:
                # Loading video_maker at the top level is causing
                # gst to be loaded at the top level, generating
                # side effects in the loader plugins. So, let's
                # move the import to the precise place where it's
                # needed.
                from . import video_maker
                video = video_maker.get_video_maker_klass()
                if ((video.has_element('vp8enc') and
                     video.has_element('webmmux'))):
                    video_file = "%s.webm" % screendump_dir
                else:
                    video_file = "%s.ogg" % screendump_dir
                video_file = os.path.join(test.debugdir, video_file)
                logging.debug("Encoding video file %s", video_file)
                video.encode(screendump_dir, video_file)

            except Exception, detail:
                logging.info(
                    "Video creation failed for %s: %s", screendump_dir, detail)

    # Warn about corrupt PPM files
    screendump_temp_dir = params.get("screendump_temp_dir")
    if screendump_temp_dir:
        screendump_temp_dir = utils_misc.get_path(
            test.bindir, screendump_temp_dir)
    else:
        screendump_temp_dir = test.debugdir
    ppm_file_rex = "*_iter%s.ppm" % test.iteration
    for f in glob.glob(os.path.join(screendump_temp_dir, ppm_file_rex)):
        if not ppm_utils.image_verify_ppm_file(f):
            logging.warn("Found corrupt PPM file: %s", f)

    # Should we convert PPM files to PNG format?
    if params.get("convert_ppm_files_to_png", "no") == "yes":
        try:
            for f in glob.glob(
                    os.path.join(screendump_temp_dir, ppm_file_rex)):
                if ppm_utils.image_verify_ppm_file(f):
                    new_path = f.replace(".ppm", ".png")
                    image = PIL.Image.open(f)
                    image.save(new_path, format='PNG')
        except NameError:
            pass

    # Should we keep the PPM files?
    if params.get("keep_ppm_files", "no") != "yes":
        for f in glob.glob(os.path.join(screendump_temp_dir, ppm_file_rex)):
            os.unlink(f)

    # Should we keep the screendump dirs?
    if params.get("keep_screendumps", "no") != "yes":
        for d in glob.glob(os.path.join(test.debugdir, "screendumps_*")):
            if os.path.isdir(d) and not os.path.islink(d):
                shutil.rmtree(d, ignore_errors=True)

    # Should we keep the video files?
    if params.get("keep_video_files", "yes") != "yes":
        for f in (glob.glob(os.path.join(test.debugdir, '*.ogg')) +
                  glob.glob(os.path.join(test.debugdir, '*.webm'))):
            os.unlink(f)

    # Terminate the register query thread
    global _vm_register_thread, _vm_register_thread_termination_event
    if _vm_register_thread is not None:
        _vm_register_thread_termination_event.set()
        _vm_register_thread.join(10)
        _vm_register_thread = None

    # Kill all unresponsive VMs
    if params.get("kill_unresponsive_vms") == "yes":
        for vm in env.get_all_vms():
            if vm.is_dead() or vm.is_paused():
                continue
            try:
                # Test may be fast, guest could still be booting
                if len(vm.virtnet) > 0:
                    session = vm.wait_for_login(timeout=vm.LOGIN_WAIT_TIMEOUT)
                    session.close()
                else:
                    session = vm.wait_for_serial_login(
                        timeout=vm.LOGIN_WAIT_TIMEOUT)
                    session.close()
            except (remote.LoginError, virt_vm.VMError, IndexError), e:
                logging.warn(e)
                vm.destroy(gracefully=False)

    # Kill VMs with deleted disks
    for vm in env.get_all_vms():
        destroy = False
        vm_params = params.object_params(vm.name)
        for image in vm_params.objects('images'):
            if params.object_params(image).get('remove_image') == 'yes':
                destroy = True
        if destroy and not vm.is_dead():
            logging.debug('Image of VM %s was removed, destroing it.', vm.name)
            vm.destroy()

    # Terminate the tcpdump thread
    env.stop_tcpdump()

    # Kill all aexpect tail threads
    aexpect.kill_tail_threads()

    living_vms = [vm for vm in env.get_all_vms() if vm.is_alive()]
    # Close all monitor socket connections of living vm.
    for vm in living_vms:
        if hasattr(vm, "monitors"):
            for m in vm.monitors:
                try:
                    m.close()
                except Exception:
                    pass
        # Close the serial console session, as it'll help
        # keeping the number of filedescriptors used by avocado-vt honest.
        vm.cleanup_serial_console()

    libvirtd_inst = utils_libvirtd.Libvirtd()
    vm_type = params.get("vm_type")

    if params.get("setup_hugepages") == "yes":
        try:
            h = test_setup.HugePageConfig(params)
            h.cleanup()
            if vm_type == "libvirt":
                libvirtd_inst.restart()
        except Exception, details:
            err += "\nHP cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if params.get("setup_thp") == "yes":
        try:
            thp = test_setup.TransparentHugePageConfig(test, params)
            thp.cleanup()
        except Exception, details:
            err += "\nTHP cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if params.get("setup_ksm") == "yes":
        try:
            ksm = test_setup.KSMConfig(params, env)
            ksm.cleanup(env)
        except Exception, details:
            err += "\nKSM cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if params.get("setup_egd") == "yes" and params.get("kill_vm") == "yes":
        try:
            egd = test_setup.EGDConfig(params, env)
            egd.cleanup()
        except Exception, details:
            err += "\negd.pl cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if vm_type == "libvirt":
        if params.get("setup_libvirt_polkit") == "yes":
            try:
                pol = test_setup.LibvirtPolkitConfig(params)
                pol.cleanup()
                libvirtd_inst.restart()
            except test_setup.PolkitConfigCleanupError, e:
                err += "\nPolkit cleanup: %s" % str(e).replace('\\n', '\n  ')
                logging.error(e)
            except Exception, details:
                err += "\nPolkit cleanup: %s" % str(details
                                                    ).replace('\\n', '\n  ')
                logging.error("Unexpected error: %s" % details)

    global kernel_cmdline, kernel_modified
    if kernel_modified and params.get("restore_kernel_cmd", "yes") == "yes":
        image_filename = storage.get_image_filename(params,
                                                    data_dir.get_data_dir())
        grub_file = params.get("grub_file", "/boot/grub2/grub.cfg")
        kernel_cfg_pos_reg = params.get("kernel_cfg_pos_reg",
                                        r".*vmlinuz-\d+.*")

        for vm in env.get_all_vms():
            if vm:
                vm.destroy()

        disk_obj = utils_disk.GuestFSModiDisk(image_filename)
        kernel_config_cur = disk_obj.read_file(grub_file)
        kernel_config = re.findall(kernel_cfg_pos_reg, kernel_config_cur)
        if not kernel_config:
            raise exceptions.TestError("Cannot find the kernel config, "
                                       "reg is %s" % kernel_cfg_pos_reg)
        kernel_config = kernel_config[0]
        disk_obj.replace_image_file_content(grub_file, kernel_config,
                                            kernel_cmdline)
        kernel_modified = False
        del disk_obj
        logging.debug("Restore the guest cmd line after test.")

    # Execute any post_commands
    if params.get("post_command"):
        try:
            process_command(test, params, env, params.get("post_command"),
                            int(params.get("post_command_timeout", "600")),
                            params.get("post_command_noncritical") == "yes")
        except Exception, details:
            err += "\nPostprocess command: %s" % str(details).replace('\n',
                                                                      '\n  ')
            logging.error(details)

    base_dir = data_dir.get_data_dir()
    if params.get("storage_type") == "iscsi":
        try:
            iscsidev = qemu_storage.Iscsidev(params, base_dir, "iscsi")
            iscsidev.cleanup()
        except Exception, details:
            err += "\niscsi cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if params.get("storage_type") == "lvm":
        try:
            lvmdev = env.get_lvmdev("lvm_%s" % params["main_vm"])
            lvmdev.cleanup()
        except Exception, details:
            err += "\nLVM cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)
        env.unregister_lvmdev("lvm_%s" % params["main_vm"])

    if params.get("storage_type") == "nfs":
        try:
            image_nfs = nfs.Nfs(params)
            image_nfs.cleanup()
        except Exception, details:
            err += "\nnfs cleanup: %s" % str(details).replace('\\n', '\n  ')

    setup_pb = False
    ovs_pb = False
    for nic in params.get('nics', "").split():
        nic_params = params.object_params(nic)
        if params.get('netdst_%s' % nic) == 'private':
            setup_pb = True
            params_pb = nic_params
            break
    else:
        setup_pb = params.get("netdst") == 'private'
        params_pb = params

    if params_pb.get("priv_br_type") == "openvswitch":
        ovs_pb = True

    if setup_pb:
        try:
            if ovs_pb:
                brcfg = test_setup.PrivateOvsBridgeConfig(params_pb)
            else:
                brcfg = test_setup.PrivateBridgeConfig(params_pb)
            brcfg.cleanup()
        except Exception, details:
            err += "\nPB cleanup: %s" % str(details).replace('\\n', '\n  ')
            logging.error(details)

    if err:
        raise virt_vm.VMError("Failures occurred while postprocess:%s" % err)
    if params.get("verify_host_dmesg", "yes") == "yes":
        utils_misc.verify_host_dmesg()


def postprocess_on_error(test, params, env):
    """
    Perform postprocessing operations required only if the test failed.

    :param test: An Autotest test object.
    :param params: A dict containing all VM and image parameters.
    :param env: The environment (a dict-like object).
    """
    params.update(params.object_params("on_error"))


def _take_screendumps(test, params, env):
    global _screendump_thread_termination_event
    temp_dir = test.debugdir
    if params.get("screendump_temp_dir"):
        temp_dir = utils_misc.get_path(test.bindir,
                                       params.get("screendump_temp_dir"))
        try:
            os.makedirs(temp_dir)
        except OSError:
            pass
    random_id = utils_misc.generate_random_string(6)
    temp_filename = "scrdump-%s-iter%s.ppm" % (random_id, test.iteration)
    temp_filename = os.path.join(temp_dir, temp_filename)
    delay = float(params.get("screendump_delay", 5))
    quality = int(params.get("screendump_quality", 30))
    inactivity_treshold = float(params.get("inactivity_treshold", 1800))
    inactivity_watcher = params.get("inactivity_watcher", "log")

    cache = {}
    counter = {}
    inactivity = {}

    while True:
        for vm in env.get_all_vms():
            if vm.instance not in counter.keys():
                counter[vm.instance] = 0
            if vm.instance not in inactivity.keys():
                inactivity[vm.instance] = time.time()
            if not vm.is_alive():
                continue
            vm_pid = vm.get_pid()
            try:
                vm.screendump(filename=temp_filename, debug=False)
            except qemu_monitor.MonitorError, e:
                logging.warn(e)
                continue
            except AttributeError, e:
                logging.warn(e)
                continue
            if not os.path.exists(temp_filename):
                logging.warn("VM '%s' failed to produce a screendump", vm.name)
                continue
            if not ppm_utils.image_verify_ppm_file(temp_filename):
                logging.warn("VM '%s' produced an invalid screendump", vm.name)
                os.unlink(temp_filename)
                continue
            screendump_dir = "screendumps_%s_%s_iter%s" % (vm.name, vm_pid,
                                                           test.iteration)
            screendump_dir = os.path.join(test.debugdir, screendump_dir)
            try:
                os.makedirs(screendump_dir)
            except OSError:
                pass
            counter[vm.instance] += 1
            filename = "%04d.jpg" % counter[vm.instance]
            screendump_filename = os.path.join(screendump_dir, filename)
            vm.verify_bsod(screendump_filename)
            image_hash = crypto.hash_file(temp_filename)
            if image_hash in cache:
                time_inactive = time.time() - inactivity[vm.instance]
                if time_inactive > inactivity_treshold:
                    msg = (
                        "%s screen is inactive for more than %d s (%d min)" %
                        (vm.name, time_inactive, time_inactive / 60))
                    if inactivity_watcher == "error":
                        try:
                            raise virt_vm.VMScreenInactiveError(vm,
                                                                time_inactive)
                        except virt_vm.VMScreenInactiveError:
                            logging.error(msg)
                            # Let's reset the counter
                            inactivity[vm.instance] = time.time()
                            test.background_errors.put(sys.exc_info())
                    elif inactivity_watcher == 'log':
                        logging.debug(msg)
                try:
                    os.link(cache[image_hash], screendump_filename)
                except OSError:
                    pass
            else:
                inactivity[vm.instance] = time.time()
                try:
                    try:
                        image = PIL.Image.open(temp_filename)
                        image.save(screendump_filename, format="JPEG",
                                   quality=quality)
                        cache[image_hash] = screendump_filename
                    except IOError, error_detail:
                        logging.warning("VM '%s' failed to produce a "
                                        "screendump: %s", vm.name, error_detail)
                        # Decrement the counter as we in fact failed to
                        # produce a converted screendump
                        counter[vm.instance] -= 1
                except NameError:
                    pass
            os.unlink(temp_filename)

        if _screendump_thread_termination_event is not None:
            if _screendump_thread_termination_event.isSet():
                _screendump_thread_termination_event = None
                break
            _screendump_thread_termination_event.wait(delay)
        else:
            # Exit event was deleted, exit this thread
            break


def store_vm_register(vm, log_filename, append=False):
    """
    Store the register information of vm into a log file

    :param vm: VM object
    :type vm: vm object
    :param log_filename: log file name
    :type log_filename: string
    :param append: Add the log to the end of the log file or not
    :type append: bool
    :return: Store the vm register information to log file or not
    :rtype: bool
    """
    try:
        output = vm.catch_monitor.info('registers', debug=False)
        timestamp = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
    except qemu_monitor.MonitorError, err:
        logging.warn(err)
        return False
    except AttributeError, err:
        logging.warn(err)
        return False

    log_filename = "%s_%s" % (log_filename, timestamp)
    if append:
        vr_log = open(log_filename, 'r+')
        vr_log.seek(0, 2)
        output += "\n"
    else:
        vr_log = open(log_filename, 'w')
    vr_log.write(output)
    vr_log.close()
    return True


def _store_vm_register(test, params, env):
    def report_result(status, results):
        msg = "%s." % status
        for vm_instance in results.keys():
            if results[vm_instance] > 0:
                msg += " Used to failed to get register info from guest"
                msg += " %s for %s times." % (vm_instance,
                                              results[vm_instance])

        if msg != "%s." % status:
            logging.debug(msg)

    global _vm_register_thread_termination_event
    delay = float(params.get("vm_register_delay", 5))
    counter = {}
    vm_register_error_count = {}
    while True:
        for vm in env.get_all_vms():
            if vm.instance not in vm_register_error_count:
                vm_register_error_count[vm.instance] = 0

            if not vm.is_alive():
                if vm_register_error_count[vm.instance] < 1:
                    logging.warning(
                        "%s is not alive. Can't query the register status" % vm.name)
                vm_register_error_count[vm.instance] += 1
                continue
            vm_pid = vm.get_pid()
            vr_dir = utils_misc.get_path(test.debugdir,
                                         "vm_register_%s_%s" % (vm.name,
                                                                vm_pid))
            try:
                os.makedirs(vr_dir)
            except OSError:
                pass

            if vm.instance not in counter:
                counter[vm.instance] = 1
            vr_filename = "%04d" % counter[vm.instance]
            vr_filename = utils_misc.get_path(vr_dir, vr_filename)
            stored_log = store_vm_register(vm, vr_filename)
            if vm_register_error_count[vm.instance] >= 1:
                logging.debug("%s alive now. Used to failed to get register"
                              " info from guest %s"
                              " times" % (vm.name,
                                          vm_register_error_count[vm.instance]))
                vm_register_error_count[vm.instance] = 0
            if stored_log:
                counter[vm.instance] += 1

        if _vm_register_thread_termination_event is not None:
            if _vm_register_thread_termination_event.isSet():
                _vm_register_thread_termination_event = None
                report_result("Thread quit", vm_register_error_count)
                break
            _vm_register_thread_termination_event.wait(delay)
        else:
            report_result("Thread quit", vm_register_error_count)
            # Exit event was deleted, exit this thread
            break
