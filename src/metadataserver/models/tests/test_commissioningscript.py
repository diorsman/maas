# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test custom commissioning scripts."""

__all__ = []

import doctest
from io import BytesIO
import json
from math import (
    ceil,
    floor,
)
import os.path
import random
from random import randint
import tarfile
from textwrap import dedent
import time
from unittest.mock import (
    create_autospec,
    Mock,
    sentinel,
)

from fixtures import FakeLogger
from maasserver.enum import (
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
)
from maasserver.fields import MAC
from maasserver.models.blockdevice import MIN_BLOCK_DEVICE_SIZE
from maasserver.models.interface import Interface
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.tag import Tag
from maasserver.models.vlan import VLAN
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.matchers import MockCalledOnceWith
from maastesting.utils import sample_binary_data
from metadataserver.enum import RESULT_TYPE
from metadataserver.fields import Bin
from metadataserver.models import (
    CommissioningScript,
    commissioningscript as cs_module,
)
from metadataserver.models.commissioningscript import (
    ARCHIVE_PREFIX,
    extract_router_mac_addresses,
    inject_lldp_result,
    inject_lshw_result,
    inject_result,
    parse_cpuinfo,
    set_virtual_tag,
    update_hardware_details,
    update_node_network_information,
    update_node_physical_block_devices,
)
from metadataserver.models.noderesult import NodeResult
from netaddr import IPNetwork
from provisioningserver.refresh.node_info_scripts import (
    LLDP_OUTPUT_NAME,
    LSHW_OUTPUT_NAME,
)
from testtools.matchers import (
    Contains,
    ContainsAll,
    DocTestMatches,
    Equals,
    MatchesStructure,
    Not,
)


def open_tarfile(content):
    """Open tar file from raw binary data."""
    return tarfile.open(fileobj=BytesIO(content))


def make_script_name(base_name=None, number=None):
    """Make up a name for a commissioning script."""
    if base_name is None:
        base_name = 'script'
    if number is None:
        number = randint(0, 99)
    return factory.make_name(
        '%0.2d-%s' % (number, factory.make_name(base_name)))


class TestCommissioningScriptManager(MAASServerTestCase):

    def test_get_archive_wraps_scripts_in_tar(self):
        script = factory.make_CommissioningScript()
        path = os.path.join(ARCHIVE_PREFIX, script.name)
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        self.assertTrue(archive.getmember(path).isfile())
        self.assertEqual(script.content, archive.extractfile(path).read())

    def test_get_archive_wraps_all_scripts(self):
        scripts = {factory.make_CommissioningScript() for counter in range(3)}
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        self.assertThat(
            archive.getnames(),
            ContainsAll({
                os.path.join(ARCHIVE_PREFIX, script.name)
                for script in scripts
                }))

    def test_get_archive_supports_binary_scripts(self):
        script = factory.make_CommissioningScript(content=sample_binary_data)
        path = os.path.join(ARCHIVE_PREFIX, script.name)
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        self.assertEqual(script.content, archive.extractfile(path).read())

    def test_get_archive_includes_builtin_scripts(self):
        name = factory.make_name('00-maas')
        path = os.path.join(ARCHIVE_PREFIX, name)
        content = factory.make_string().encode('ascii')
        data = dict(name=name, content=content, hook='hook')
        self.patch(cs_module, 'NODE_INFO_SCRIPTS', {name: data})
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        self.assertIn(path, archive.getnames())
        self.assertEqual(content, archive.extractfile(path).read())

    def test_get_archive_sets_sensible_mode(self):
        for counter in range(3):
            factory.make_CommissioningScript()
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        self.assertEqual({0o755}, {info.mode for info in archive.getmembers()})

    def test_get_archive_initializes_file_timestamps(self):
        # The mtime on a file inside the tarball is reasonable.
        # It would otherwise default to the Epoch, and GNU tar warns
        # annoyingly about improbably old files.
        start_time = floor(time.time())
        script = factory.make_CommissioningScript()
        path = os.path.join(ARCHIVE_PREFIX, script.name)
        archive = open_tarfile(CommissioningScript.objects.get_archive())
        timestamp = archive.getmember(path).mtime
        end_time = ceil(time.time())
        self.assertGreaterEqual(timestamp, start_time)
        self.assertLessEqual(timestamp, end_time)


class TestCommissioningScript(MAASServerTestCase):

    def test_scripts_may_be_binary(self):
        name = make_script_name()
        CommissioningScript.objects.create(
            name=name, content=Bin(sample_binary_data))
        stored_script = CommissioningScript.objects.get(name=name)
        self.assertEqual(sample_binary_data, stored_script.content)


lldp_output_template = """
<?xml version="1.0" encoding="UTF-8"?>
<lldp label="LLDP neighbors">
%s
</lldp>
"""

lldp_output_interface_template = """
<interface label="Interface" name="eth1" via="LLDP">
  <chassis label="Chassis">
    <id label="ChassisID" type="mac">%s</id>
    <name label="SysName">switch-name</name>
    <descr label="SysDescr">HDFD5BG7J</descr>
    <mgmt-ip label="MgmtIP">192.168.9.9</mgmt-ip>
    <capability label="Capability" type="Bridge" enabled="on"/>
    <capability label="Capability" type="Router" enabled="off"/>
  </chassis>
</interface>
"""


def make_lldp_output(macs):
    """Return an example raw lldp output containing the given MACs."""
    interfaces = '\n'.join(
        lldp_output_interface_template % mac
        for mac in macs
        )
    script = (lldp_output_template % interfaces).encode('utf8')
    return bytes(script)


class TestExtractRouters(MAASServerTestCase):

    def test_extract_router_mac_addresses_returns_None_when_empty_input(self):
        self.assertIsNone(extract_router_mac_addresses(''))

    def test_extract_router_mac_addresses_returns_empty_list(self):
        lldp_output = make_lldp_output([])
        self.assertItemsEqual([], extract_router_mac_addresses(lldp_output))

    def test_extract_router_mac_addresses_returns_routers_list(self):
        macs = ["11:22:33:44:55:66", "aa:bb:cc:dd:ee:ff"]
        lldp_output = make_lldp_output(macs)
        routers = extract_router_mac_addresses(lldp_output)
        self.assertItemsEqual(macs, routers)


class TestInjectResult(MAASServerTestCase):

    def test_inject_result_stores_data(self):
        node = factory.make_Node()
        name = factory.make_name("result")
        output = factory.make_bytes()
        exit_status = next(factory.random_octets)

        inject_result(node, name, output, exit_status)

        self.assertThat(
            NodeResult.objects.get(node=node, name=name),
            MatchesStructure.byEquality(
                node=node, name=name, script_result=exit_status,
                result_type=RESULT_TYPE.COMMISSIONING,
                data=output))

    def test_inject_result_calls_hook(self):
        node = factory.make_Node()
        name = factory.make_name("result")
        output = factory.make_bytes()
        exit_status = next(factory.random_octets)
        hook = Mock()
        self.patch(
            cs_module, "NODE_INFO_SCRIPTS",
            {name: {"hook": hook}})

        inject_result(node, name, output, exit_status)

        self.assertThat(hook, MockCalledOnceWith(
            node=node, output=output, exit_status=exit_status))

    def inject_lshw_result(self):
        # inject_lshw_result() just calls through to inject_result().
        inject_result = self.patch(
            cs_module, "inject_result",
            create_autospec(cs_module.inject_result))
        inject_lshw_result(sentinel.node, sentinel.output, sentinel.status)
        self.assertThat(inject_result, MockCalledOnceWith(
            sentinel.node, LSHW_OUTPUT_NAME, sentinel.output, sentinel.status))

    def inject_lldp_result(self):
        # inject_lldp_result() just calls through to inject_result().
        inject_result = self.patch(
            cs_module, "inject_result",
            create_autospec(cs_module.inject_result))
        inject_lldp_result(sentinel.node, sentinel.output, sentinel.status)
        self.assertThat(inject_result, MockCalledOnceWith(
            sentinel.node, LLDP_OUTPUT_NAME, sentinel.output, sentinel.status))


class TestSetVirtualTag(MAASServerTestCase):

    def getVirtualTag(self):
        virtual_tag, _ = Tag.objects.get_or_create(name='virtual')
        return virtual_tag

    def assertTagsEqual(self, node, tags):
        self.assertItemsEqual(
            tags, [tag.name for tag in node.tags.all()])

    def test_sets_virtual_tag(self):
        node = factory.make_Node()
        self.assertTagsEqual(node, [])
        set_virtual_tag(node, b"virtual", 0)
        self.assertTagsEqual(node, ["virtual"])

    def test_removes_virtual_tag(self):
        node = factory.make_Node()
        node.tags.add(self.getVirtualTag())
        self.assertTagsEqual(node, ["virtual"])
        set_virtual_tag(node, b"notvirtual", 0)
        self.assertTagsEqual(node, [])

    def test_output_not_containing_virtual_does_not_set_tag(self):
        logger = self.useFixture(FakeLogger())
        node = factory.make_Node()
        self.assertTagsEqual(node, [])
        set_virtual_tag(node, b"wibble", 0)
        self.assertTagsEqual(node, [])
        self.assertIn(
            "Neither 'virtual' nor 'notvirtual' appeared in the captured "
            "VIRTUALITY_SCRIPT output for node %s.\n" % node.system_id,
            logger.output)

    def test_output_not_containing_virtual_does_not_remove_tag(self):
        logger = self.useFixture(FakeLogger())
        node = factory.make_Node()
        node.tags.add(self.getVirtualTag())
        self.assertTagsEqual(node, ["virtual"])
        set_virtual_tag(node, b"wibble", 0)
        self.assertTagsEqual(node, ["virtual"])
        self.assertIn(
            "Neither 'virtual' nor 'notvirtual' appeared in the captured "
            "VIRTUALITY_SCRIPT output for node %s.\n" % node.system_id,
            logger.output)


class TestUpdateHardwareDetails(MAASServerTestCase):

    doctest_flags = doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE

    def test_hardware_updates_cpu_count(self):
        node = factory.make_Node()
        xmlbytes = dedent("""\
        <node id="core">
           <node id="cpu:0" class="processor"/>
           <node id="cpu:1" class="processor"/>
        </node>
        """).encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        self.assertEqual(2, node.cpu_count)

    def test_cpu_count_counts_multi_cores(self):
        node = factory.make_Node()
        xmlbytes = dedent("""\
        <node id="core">
           <node id="cpu:0" class="processor">
             <configuration>
               <setting id="cores" value="2" />
               <setting id="enabledcores" value="2" />
               <setting id="threads" value="4" />
             </configuration>
           </node>
           <node id="cpu:1" class="processor"/>
        </node>
        """).encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        self.assertEqual(5, node.cpu_count)

    def test_cpu_count_skips_disabled_cpus(self):
        node = factory.make_Node()
        xmlbytes = dedent("""\
        <node id="core">
           <node id="cpu:0" class="processor"/>
           <node id="cpu:1" disabled="true" class="processor"/>
           <node id="cpu:2" disabled="true" class="processor"/>
        </node>
        """).encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        self.assertEqual(1, node.cpu_count)

    def test_hardware_updates_memory(self):
        node = factory.make_Node()
        xmlbytes = dedent("""\
        <node id="memory">
           <size units="bytes">4294967296</size>
        </node>
        """).encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        self.assertEqual(4096, node.memory)

    def test_hardware_updates_memory_lenovo(self):
        node = factory.make_Node()
        xmlbytes = dedent("""\
        <node>
          <node id="memory:0" class="memory">
            <node id="bank:0" class="memory" handle="DMI:002D">
              <size units="bytes">4294967296</size>
            </node>
            <node id="bank:1" class="memory" handle="DMI:002E">
              <size units="bytes">3221225472</size>
            </node>
          </node>
          <node id="memory:1" class="memory">
            <node id="bank:0" class="memory" handle="DMI:002F">
              <size units="bytes">536870912</size>
            </node>
          </node>
          <node id="memory:2" class="memory"></node>
        </node>
        """).encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        mega = 2 ** 20
        expected = (4294967296 + 3221225472 + 536879812) // mega
        self.assertEqual(expected, node.memory)

    def test_hardware_updates_ignores_empty_tags(self):
        # Tags with empty definitions are ignored when
        # update_hardware_details gets called.
        factory.make_Tag(definition='')
        node = factory.make_Node()
        node.save()
        xmlbytes = '<node/>'.encode("utf-8")
        update_hardware_details(node, xmlbytes, 0)
        node = reload_object(node)
        # The real test is that update_hardware_details does not blow
        # up, see bug 1131418.
        self.assertEqual([], list(node.tags.all()))

    def test_hardware_updates_logs_invalid_xml(self):
        logger = self.useFixture(FakeLogger())
        update_hardware_details(factory.make_Node(), b"garbage", 0)
        expected_log = dedent("""\
        Invalid lshw data.
        Traceback (most recent call last):
        ...
        lxml.etree.XMLSyntaxError: Start tag expected, ...
        """)
        self.assertThat(
            logger.output, DocTestMatches(
                expected_log, self.doctest_flags))

    def test_hardware_updates_does_nothing_when_exit_status_is_not_zero(self):
        logger = self.useFixture(FakeLogger(name='commissioningscript'))
        update_hardware_details(factory.make_Node(), b"garbage", exit_status=1)
        self.assertEqual("", logger.output)


class TestParseCPUInfo(MAASServerTestCase):

    doctest_flags = doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE

    def test_parse_cpuinfo(self):
        node = factory.make_Node()
        cpuinfo = dedent("""\
        processor\t: 0
        vendor_id\t: GenuineIntel

        processor\t: 1
        vendor_id\t: GenuineIntel
        """).encode('utf-8')
        parse_cpuinfo(node, cpuinfo, 0)
        self.assertEqual(2, reload_object(node).cpu_count)


class TestUpdateNodePhysicalBlockDevices(MAASServerTestCase):

    def make_block_device(
            self, name=None, path=None, id_path=None, size=None,
            block_size=None, model=None, serial=None, rotary=True, rpm=None,
            removable=False, sata=False):
        if name is None:
            name = factory.make_name('name')
        if path is None:
            path = '/dev/%s' % name
        if id_path is None:
            id_path = '/dev/disk/by-id/deviceid'
        if size is None:
            size = random.randint(
                MIN_BLOCK_DEVICE_SIZE * 10, MIN_BLOCK_DEVICE_SIZE * 100)
        if block_size is None:
            block_size = random.choice([512, 1024, 4096])
        if model is None:
            model = factory.make_name('model')
        if serial is None:
            serial = factory.make_name('serial')
        if rpm is None:
            rpm = random.choice(('4800', '5400', '10000', '15000'))
        return {
            "NAME": name,
            "PATH": path,
            "ID_PATH": id_path,
            "SIZE": '%s' % size,
            "BLOCK_SIZE": '%s' % block_size,
            "MODEL": model,
            "SERIAL": serial,
            "RO": "0",
            "RM": "1" if removable else "0",
            "ROTA": "1" if rotary else "0",
            "SATA": "1" if sata else "0",
            "RPM": "0" if not rotary else rpm
            }

    def test__does_nothing_when_exit_status_is_not_zero(self):
        node = factory.make_Node()
        block_device = factory.make_PhysicalBlockDevice(node=node)
        update_node_physical_block_devices(node, b"garbage", exit_status=1)
        self.assertIsNotNone(reload_object(block_device))

    def test__does_nothing_if_skip_storage(self):
        node = factory.make_Node(skip_storage=True)
        block_device = factory.make_PhysicalBlockDevice(node=node)
        update_node_physical_block_devices(node, b"garbage", exit_status=0)
        self.assertIsNotNone(reload_object(block_device))

    def test__removes_previous_physical_block_devices(self):
        node = factory.make_Node()
        block_device = factory.make_PhysicalBlockDevice(node=node)
        update_node_physical_block_devices(node, b"[]", 0)
        self.assertIsNone(reload_object(block_device))

    def test__creates_physical_block_devices(self):
        devices = [self.make_block_device() for _ in range(3)]
        device_names = [device['NAME'] for device in devices]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        created_names = [
            device.name
            for device in PhysicalBlockDevice.objects.filter(node=node)
            ]
        self.assertItemsEqual(device_names, created_names)

    def test__handles_renamed_block_device(self):
        devices = [self.make_block_device(name='sda', serial='first')]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        devices = [
            self.make_block_device(name='sda', serial='second'),
            self.make_block_device(name='sdb', serial='first'),
        ]
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        device_names = [device['NAME'] for device in devices]
        created_names = [
            device.name
            for device in PhysicalBlockDevice.objects.filter(node=node)
            ]
        self.assertItemsEqual(device_names, created_names)

    def test__only_updates_physical_block_devices(self):
        devices = [self.make_block_device() for _ in range(3)]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        created_ids_one = [
            device.id
            for device in PhysicalBlockDevice.objects.filter(node=node)
            ]
        update_node_physical_block_devices(node, json_output, 0)
        created_ids_two = [
            device.id
            for device in PhysicalBlockDevice.objects.filter(node=node)
            ]
        self.assertItemsEqual(created_ids_two, created_ids_one)

    def test__doesnt_reset_boot_disk(self):
        devices = [self.make_block_device() for _ in range(3)]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        boot_disk = PhysicalBlockDevice.objects.filter(node=node).first()
        node.boot_disk = boot_disk
        node.save()
        update_node_physical_block_devices(node, json_output, 0)
        self.assertEqual(boot_disk, reload_object(node).boot_disk)

    def test__clears_boot_disk(self):
        devices = [self.make_block_device() for _ in range(3)]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        update_node_physical_block_devices(
            node, json.dumps([]).encode('utf-8'), 0)
        self.assertIsNone(reload_object(node).boot_disk)

    def test__creates_physical_block_devices_in_order(self):
        devices = [self.make_block_device() for _ in range(3)]
        device_names = [device['NAME'] for device in devices]
        node = factory.make_Node()
        json_output = json.dumps(devices).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        created_names = [
            device.name
            for device in (
                PhysicalBlockDevice.objects.filter(node=node).order_by('id'))
            ]
        self.assertEqual(device_names, created_names)

    def test__creates_physical_block_device(self):
        name = factory.make_name('name')
        id_path = '/dev/disk/by-id/deviceid'
        size = random.randint(MIN_BLOCK_DEVICE_SIZE, 1000 * 1000 * 1000)
        block_size = random.choice([512, 1024, 4096])
        model = factory.make_name('model')
        serial = factory.make_name('serial')
        device = self.make_block_device(
            name=name, size=size, block_size=block_size,
            model=model, serial=serial)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first(),
            MatchesStructure.byEquality(
                name=name, id_path=id_path, size=size,
                block_size=block_size, model=model, serial=serial))

    def test__creates_physical_block_device_with_path(self):
        name = factory.make_name('name')
        size = random.randint(MIN_BLOCK_DEVICE_SIZE, 1000 * 1000 * 1000)
        block_size = random.choice([512, 1024, 4096])
        model = factory.make_name('model')
        serial = factory.make_name('serial')
        device = self.make_block_device(
            name=name, size=size, block_size=block_size,
            model=model, serial=serial, id_path='')
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first(),
            MatchesStructure.byEquality(
                name=name, id_path='/dev/%s' % name, size=size,
                block_size=block_size, model=model, serial=serial))

    def test__creates_physical_block_device_only_for_node(self):
        device = self.make_block_device()
        node = factory.make_Node(with_boot_disk=False)
        other_node = factory.make_Node(with_boot_disk=False)
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertEqual(
            0, PhysicalBlockDevice.objects.filter(node=other_node).count(),
            "Created physical block device for the incorrect node.")

    def test__creates_physical_block_device_with_rotary_tag(self):
        device = self.make_block_device(rotary=True)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Contains('rotary'))
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Not(Contains('ssd')))

    def test__creates_physical_block_device_with_rotary_and_rpm_tags(self):
        device = self.make_block_device(rotary=True, rpm=5400)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Contains('rotary'))
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Contains('5400rpm'))

    def test__creates_physical_block_device_with_ssd_tag(self):
        device = self.make_block_device(rotary=False)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            ContainsAll(['ssd']))
        self.expectThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Not(Contains('rotary')))

    def test__creates_physical_block_device_without_removable_tag(self):
        device = self.make_block_device(removable=False)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Not(Contains('removable')))

    def test__creates_physical_block_device_with_removable_tag(self):
        device = self.make_block_device(removable=True)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Contains('removable'))

    def test__creates_physical_block_device_without_sata_tag(self):
        device = self.make_block_device(sata=False)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Not(Contains('sata')))

    def test__creates_physical_block_device_with_sata_tag(self):
        device = self.make_block_device(sata=True)
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertThat(
            PhysicalBlockDevice.objects.filter(node=node).first().tags,
            Contains('sata'))

    def test__ignores_min_block_device_size_devices(self):
        device = self.make_block_device(
            size=random.randint(1, MIN_BLOCK_DEVICE_SIZE))
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertEquals(
            0, len(PhysicalBlockDevice.objects.filter(node=node)))

    def test__ignores_loop_devices(self):
        device = self.make_block_device(id_path='/dev/loop0')
        node = factory.make_Node()
        json_output = json.dumps([device]).encode('utf-8')
        update_node_physical_block_devices(node, json_output, 0)
        self.assertEquals(
            0, len(PhysicalBlockDevice.objects.filter(node=node)))


class TestUpdateNodeNetworkInformation(MAASServerTestCase):
    """Tests the update_node_network_information function using data from the
    ip_addr_results.txt file to simulate `ip addr`'s output.

    The EXPECTED_MACS dictionary below must match the contents of the file,
    which should specify a list of physical interfaces (such as what would
    be expected to be found during commissioning).
    """

    EXPECTED_INTERFACES = {
        'eth0': MAC("00:00:00:00:00:01"),
        'eth1': MAC("00:00:00:00:00:02"),
        'eth2': MAC("00:00:00:00:00:03"),
    }

    EXPECTED_INTERFACES_XENIAL = {
        'ens3': MAC("52:54:00:2d:39:49"),
        'ens10': MAC("52:54:00:e5:c6:6b"),
        'ens11': MAC("52:54:00:ed:9f:9d"),
    }

    IP_ADDR_OUTPUT_FILE = os.path.join(
        os.path.dirname(__file__), 'ip_addr_results.txt')
    with open(IP_ADDR_OUTPUT_FILE, "rb") as fd:
        IP_ADDR_OUTPUT = fd.read()
    IP_ADDR_OUTPUT_FILE = os.path.join(
        os.path.dirname(__file__), 'ip_addr_results_xenial.txt')
    with open(IP_ADDR_OUTPUT_FILE, "rb") as fd:
        IP_ADDR_OUTPUT_XENIAL = fd.read()

    def assert_expected_interfaces_and_macs_exist(
            self, node_interfaces, additional_interfaces={},
            expected_interfaces=EXPECTED_INTERFACES):
        """Asserts to ensure that the type, name, and MAC address are
        appropriate, given Node's interfaces. (and an optional list of
        additional interfaces which must exist)
        """
        expected_interfaces = expected_interfaces.copy()
        expected_interfaces.update(additional_interfaces)

        self.assertThat(len(node_interfaces), Equals(len(expected_interfaces)))

        for interface in node_interfaces:
            if (interface.name.startswith('eth') or
                    interface.name.startswith('ens')):
                parts = interface.name.split('.')
                if len(parts) == 2 and parts[1].isdigit():
                    iftype = INTERFACE_TYPE.VLAN
                else:
                    iftype = INTERFACE_TYPE.PHYSICAL
                self.assertThat(
                    interface.type, Equals(iftype))
            self.assertIn(interface.name, expected_interfaces)
            self.assertThat(interface.mac_address, Equals(
                expected_interfaces[interface.name]))

    def test__does_nothing_if_skip_networking(self):
        node = factory.make_Node(interface=True, skip_networking=True)
        boot_interface = node.get_boot_interface()
        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)
        self.assertIsNotNone(reload_object(boot_interface))

    def test__add_all_interfaces(self):
        """Test a node that has no previously known interfaces on which we
        need to add a series of interfaces.
        """
        node = factory.make_Node()

        # Delete all Interfaces created by factory attached to this node.
        Interface.objects.filter(node_id=node.id).delete()

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        # Makes sure all the test dataset MAC addresses were added to the node.
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

    def test__add_all_interfaces_xenial(self):
        """Test a node that has no previously known interfaces on which we
        need to add a series of interfaces.
        """
        node = factory.make_Node()

        # Delete all Interfaces created by factory attached to this node.
        Interface.objects.filter(node_id=node.id).delete()

        update_node_network_information(node, self.IP_ADDR_OUTPUT_XENIAL, 0)

        # Makes sure all the test dataset MAC addresses were added to the node.
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(
            node_interfaces,
            expected_interfaces=self.EXPECTED_INTERFACES_XENIAL)

    def test__one_mac_missing(self):
        """Test whether we correctly detach a NIC that no longer appears to be
        connected to the node.
        """
        node = factory.make_Node()

        # Create a MAC address that we know is not in the test dataset.
        factory.make_Interface(
            node=node, mac_address="01:23:45:67:89:ab")

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        # These should have been added to the node.
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

        # This one should have been removed because it no longer shows on the
        # `ip addr` output.
        db_macaddresses = [
            iface.mac_address for iface in node.interface_set.all()
            ]
        self.assertNotIn(MAC('01:23:45:67:89:ab'), db_macaddresses)

    def test__reassign_mac(self):
        """Test whether we can assign a MAC address previously connected to a
        different node to the current one"""
        node1 = factory.make_Node()

        # Create a MAC address that we know IS in the test dataset.
        interface_to_be_reassigned = factory.make_Interface(node=node1)
        interface_to_be_reassigned.mac_address = MAC('00:00:00:00:00:01')
        interface_to_be_reassigned.save()

        node2 = factory.make_Node()
        update_node_network_information(node2, self.IP_ADDR_OUTPUT, 0)

        node2_interfaces = Interface.objects.filter(node=node2)
        self.assert_expected_interfaces_and_macs_exist(node2_interfaces)

        # Ensure the MAC object moved over to node2.
        self.assertItemsEqual([], Interface.objects.filter(node=node1))
        self.assertItemsEqual([], Interface.objects.filter(node=node1))

    def test__reassign_interfaces(self):
        """Test whether we can assign interfaces previously connected to a
        different node to the current one"""
        node1 = factory.make_Node()
        update_node_network_information(node1, self.IP_ADDR_OUTPUT, 0)

        # First make sure the first node has all the expected interfaces.
        node2_interfaces = Interface.objects.filter(node=node1)
        self.assert_expected_interfaces_and_macs_exist(node2_interfaces)

        # Grab the id from one of the created interfaces.
        interface_id = Interface.objects.filter(node=node1).first().id

        # Now make sure the second node has them all.
        node2 = factory.make_Node()
        update_node_network_information(node2, self.IP_ADDR_OUTPUT, 0)

        node2_interfaces = Interface.objects.filter(node=node2)
        self.assert_expected_interfaces_and_macs_exist(node2_interfaces)

        # Now make sure all the objects moved to the second node.
        self.assertItemsEqual([], Interface.objects.filter(node=node1))
        self.assertItemsEqual([], Interface.objects.filter(node=node1))

        # ... and ensure that the interface was deleted.
        self.assertItemsEqual([], Interface.objects.filter(id=interface_id))

    def test__does_not_delete_virtual_interfaces_with_shared_mac(self):
        # Note: since this VLANInterface will be linked to the default VLAN
        # ("vid 0", which is actually invalid) the VLANInterface will
        # automatically get the name "vlan0".
        ETH0_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        ETH1_MAC = self.EXPECTED_INTERFACES['eth1'].get_raw()
        BOND_NAME = 'bond0'
        node = factory.make_Node()

        eth0 = factory.make_Interface(
            name="eth0", mac_address=ETH0_MAC, node=node)
        eth1 = factory.make_Interface(
            name="eth1", mac_address=ETH1_MAC, node=node)

        vlanif = factory.make_Interface(
            INTERFACE_TYPE.VLAN, mac_address=ETH0_MAC, parents=[eth0],
            node=node)
        factory.make_Interface(
            INTERFACE_TYPE.BOND, mac_address=ETH1_MAC, parents=[eth1],
            node=node, name=BOND_NAME)

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(
            node_interfaces, {vlanif.name: ETH0_MAC, BOND_NAME: ETH1_MAC})

    def test__interface_names_changed(self):
        # Note: the MACs here are swapped compared to their expected values.
        ETH0_MAC = self.EXPECTED_INTERFACES['eth1'].get_raw()
        ETH1_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        node = factory.make_Node()

        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, name="eth0", mac_address=ETH0_MAC,
            node=node)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, name="eth1", mac_address=ETH1_MAC,
            node=node)

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        node_interfaces = Interface.objects.filter(node=node)
        # This will ensure that the interfaces were renamed appropriately.
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

    def test__mac_id_is_preserved(self):
        """Test whether MAC address entities are preserved and not recreated"""
        ETH0_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        node = factory.make_Node()
        iface_to_be_preserved = factory.make_Interface(
            mac_address=ETH0_MAC, node=node)

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        self.assertIsNotNone(reload_object(iface_to_be_preserved))

    def test__legacy_model_upgrade_preserves_interfaces(self):
        ETH0_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        ETH1_MAC = self.EXPECTED_INTERFACES['eth1'].get_raw()
        node = factory.make_Node()
        eth0 = factory.make_Interface(mac_address=ETH0_MAC, node=node)
        eth1 = factory.make_Interface(mac_address=ETH1_MAC, node=node)

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        self.assertEqual(eth0, Interface.objects.get(id=eth0.id))
        self.assertEqual(eth1, Interface.objects.get(id=eth1.id))

        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

    def test__legacy_model_with_extra_mac(self):
        ETH0_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        ETH1_MAC = self.EXPECTED_INTERFACES['eth1'].get_raw()
        ETH2_MAC = self.EXPECTED_INTERFACES['eth2'].get_raw()
        ETH3_MAC = '00:00:00:00:01:04'
        node = factory.make_Node()
        eth0 = factory.make_Interface(mac_address=ETH0_MAC, node=node)
        eth1 = factory.make_Interface(mac_address=ETH1_MAC, node=node)
        eth2 = factory.make_Interface(mac_address=ETH2_MAC, node=node)
        eth3 = factory.make_Interface(mac_address=ETH3_MAC, node=node)

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)

        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

        # Make sure we re-used the existing MACs in the database.
        self.assertIsNotNone(reload_object(eth0))
        self.assertIsNotNone(reload_object(eth1))
        self.assertIsNotNone(reload_object(eth2))

        # Make sure the interface that no longer exists has been removed.
        self.assertIsNone(reload_object(eth3))

    def test__does_not_delete_virtual_interfaces_with_unique_mac(self):
        ETH0_MAC = self.EXPECTED_INTERFACES['eth0'].get_raw()
        ETH1_MAC = self.EXPECTED_INTERFACES['eth1'].get_raw()
        BOND_MAC = '00:00:00:00:01:02'
        node = factory.make_Node()
        eth0 = factory.make_Interface(mac_address=ETH0_MAC, node=node)
        eth1 = factory.make_Interface(mac_address=ETH1_MAC, node=node)
        vlan = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[eth0])
        bond = factory.make_Interface(
            INTERFACE_TYPE.BOND, mac_address=BOND_MAC, node=node,
            parents=[eth1])

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)
        # Freshen the other objects, since they may have changed names.
        vlan = reload_object(vlan)
        bond = reload_object(bond)
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(
            node_interfaces, {vlan.name: ETH0_MAC, bond.name: BOND_MAC})

    def test__deletes_virtual_interfaces_linked_to_removed_macs(self):
        VLAN_MAC = '00:00:00:00:01:01'
        BOND_MAC = '00:00:00:00:01:02'
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            name='eth0', mac_address=VLAN_MAC, node=node)
        eth1 = factory.make_Interface(
            name='eth1', mac_address=BOND_MAC, node=node)
        factory.make_Interface(
            INTERFACE_TYPE.VLAN, mac_address=VLAN_MAC, parents=[eth0])
        factory.make_Interface(
            INTERFACE_TYPE.BOND, mac_address=BOND_MAC, parents=[eth1])

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)
        node_interfaces = Interface.objects.filter(node=node)
        self.assert_expected_interfaces_and_macs_exist(node_interfaces)

    def test__creates_discovered_ip_address(self):
        node = factory.make_Node()
        cidr = '192.168.0.3/24'
        subnet = factory.make_Subnet(
            cidr=cidr, vlan=VLAN.objects.get_default_vlan())

        update_node_network_information(node, self.IP_ADDR_OUTPUT, 0)
        eth0 = Interface.objects.get(node=node, name='eth0')
        address = str(IPNetwork(cidr).ip)
        ipv4_ip = eth0.ip_addresses.get(ip=address)
        self.assertThat(
            ipv4_ip,
            MatchesStructure.byEquality(
                alloc_type=IPADDRESS_TYPE.DISCOVERED, subnet=subnet,
                ip=address))

    def test__creates_discovered_ip_address_on_xenial(self):
        node = factory.make_Node()
        cidr = '172.16.100.108/24'
        subnet = factory.make_Subnet(
            cidr=cidr, vlan=VLAN.objects.get_default_vlan())

        update_node_network_information(node, self.IP_ADDR_OUTPUT_XENIAL, 0)
        eth0 = Interface.objects.get(node=node, name='ens3')
        address = str(IPNetwork(cidr).ip)
        ipv4_ip = eth0.ip_addresses.get(ip=address)
        self.assertThat(
            ipv4_ip,
            MatchesStructure.byEquality(
                alloc_type=IPADDRESS_TYPE.DISCOVERED, subnet=subnet,
                ip=address))
        self.assertThat(eth0.ip_addresses.count(), Equals(1))
