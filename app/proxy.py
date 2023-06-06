import requests,pynetbox,random, json, threading, ipaddress
from flask import Flask, request, jsonify, copy_current_request_context
from telnetlib import Telnet
from napalm import get_network_driver
from jinja2 import Template
from string import Template
application = Flask(__name__)
project_id = "eb9147b9-15d6-4a12-96f8-df230916f593"
netbox_token = '0123456789abcdef0123456789abcdef01234567'
gns_url = "gns3.domski.tech:3080"
nb = pynetbox.api('http://gns3.domski.tech:8000/', token=netbox_token)


@application.post("/device")
def device():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415

    # Get initial call from netbox webhook when device is created
    device = request.get_json()

    # Create device in another thread
    threading.Thread(target=create_device, name="configure_device", kwargs=device).start()

    # Happy return code back to netbox
    return "Node was created", 201

def create_device(**device):

    # Set variables accordingly
    id = device['data']['id']
    name = device['data']['name']
    device_type = nb.dcim.device_types.get(id=device['data']['device_type']['id'])
    template_id = device_type['slug']
    conf = device_type['custom_fields']['ztp_config']

    # Make API call to GNS3 to create the VM
    api_url = f"http://{gns_url}/v2/projects/{project_id}/templates/{template_id}"
    data = {"x": random.randrange(-800,800), "y": random.randrange(-500,500), "name": f"{name}", "compute_id": "local"}
    response = requests.post(api_url, json=data)

    # Extract GNS3 assigned data
    node_id = response.json()["node_id"]
    console = response.json()["console"]

    # Generate mac address for mgmt nic
    r1 = random.randint(0, 255)
    r2 = random.randint(0, 255)
    r3 = random.randint(0, 255)
    mac_address = "00:20:91:%02x:%02x:%02x" % (r1,r2,r3)
    is_is_id = "0020.91%02x,%02x%02x" % (r1,r2,r3)
    option = device_type['custom_fields']['options']
    options = f"-nic bridge,br=br0,model=e1000,mac={mac_address}{option}"

    # Make API call to update the VM's name and Mgmt nic in GNS3
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}"
    data = {"name": name, "properties": { "options": options } }
    requests.put(api_url, json=data)

    # Start VM
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}/start"
    requests.post(api_url)

    # Allocate a mgmt ip address
    primary_ip4 = nb.ipam.prefixes.get(2).available_ips.create() # 2 is the Mgmt prefix

    # Find mgmt interface ID
    int_id = nb.dcim.interfaces.get(device_id=id,name="MgmtEth0/0/CPU0/0")['id']

    # Set the mgmt interface vrf and mac address
    nb.dcim.interfaces.update([{'id': int_id, 'vrf': 1, 'mac_address': mac_address}])

    # Assign mgmt IP to the mgmt interface
    nb.ipam.ip_addresses.update([{'id': primary_ip4.id, 'vrf': 1, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': int_id}])

    # Update netbox console port and node_id
    nb.dcim.devices.update([{'id': id, 'serial': node_id, 'custom_fields': {'console': console, 'is_is_system_id': is_is_id}, 'primary_ip4': primary_ip4.id, 'status': "planned"}])
    
    # Connect with telnet and begin configuring
    tn = Telnet('gns3.domski.tech', console)
    for line in conf['conf']:
        if "timeout" in line:
            tn.read_until(line['read'].encode('utf-8'), timeout=line['timeout'])
        else:
            tn.read_until(line['read'].encode('utf-8'))
        rendered = Template(line['write'])
        tn.write(rendered.substitute(name=name,ip=primary_ip4).encode('utf-8'))
    tn.close()
    
    # Set status to active after (hopefully) successful config
    nb.dcim.devices.update([{'id': id, 'status': "active"}])

@application.delete("/device")
def device_delete():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415

    # Get initial call from netbox webhook when device is created
    device = request.get_json()

    # Create device in another thread
    threading.Thread(target=delete_device, name="configure_device", kwargs=device).start()

    # Happy return code back to netbox
    return "Node was created", 201

def delete_device(**device):

    # find device ID
    id = device['data']['id']
    
    # Find the device's node_id from the call's json body
    node_id = device['data']['serial']

    # Turn off th router
    requests.post(f"http://{gns_url}/v2/projects/{project_id}/nodes/{device['data']['serial']}/stop")
    name = device['data']['name']

    # Delete associated cables 
    nb.dcim.cables.delete(nb.dcim.cables.filter(device_id=id))

    # Delete associated IP addresses
    nb.ipam.ip_addresses.delete(nb.ipam.ip_addresses.filter(device_id=id))

    # Delete the router
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}"
    requests.delete(api_url)

    # Happy return code back to netbox
    return f"{name} was deleted", 201

@application.post("/cable")
def cable():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415
    
    cable = request.get_json()

    # Get necessary data from netbox
    device_a = nb.dcim.devices.get(id=cable['data']['a_terminations'][0]['object']['device']['id'])
    device_b = nb.dcim.devices.get(id=cable['data']['b_terminations'][0]['object']['device']['id'])
    interface_a = nb.dcim.interfaces.get(id=cable['data']['a_terminations'][0]['object_id'])
    interface_b = nb.dcim.interfaces.get(id=cable['data']['b_terminations'][0]['object_id'])

    # Make API call to create the cable in GNS3
    api_url = f"http://{gns_url}/v2/projects/{project_id}/links"
    data = {"nodes": [{ "node_id": device_a['serial'], "adapter_number": int(interface_a['label']), "port_number": 0 }, { "node_id": device_b['serial'], "adapter_number": int(interface_b['label']), "port_number": 0 }]}
    response = requests.post(api_url, json=data)

    # Control logic for determining the link type
    # if device_a['device_role']['id'] == 3:
    #     # If a is a core..
    #     if device_b['device_role']['id'] == 3:
    #         # core to core, do is-is (tag 1 is is-is)
    #         nb.dcim.interfaces.update([{'id': cable['data']['a_terminations'][0]['object_id'], 'vrf': 2, 'tags': [{'id': 1}]}])
    #         nb.dcim.interfaces.update([{'id': cable['data']['b_terminations'][0]['object_id'], 'vrf': 2, 'tags': [{'id': 1}]}])

    #         # generate v6 prefix for link, 6 is the prefix to allocate /127s from. should be chosen dynamically
    #         prefix = nb.ipam.prefixes.get(6).available_prefixes.create({"prefix_length": 127})
    #         # generate ip addresses from prefix
    #         ip_a_side = prefix.available_ips.create()
    #         ip_b_side = prefix.available_ips.create()

    #         # push ip address changes to Netbox
    #         nb.ipam.ip_addresses.update([{'id': ip_a_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['a_terminations'][0]['object_id']}])
    #         nb.ipam.ip_addresses.update([{'id': ip_b_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['b_terminations'][0]['object_id']}])

    # Update netbox with the cable ID
    nb.dcim.cables.update([{'id': cable['data']['id'], 'label': response.json()["link_id"]}])

    # Happy return code back to netbox
    return f"", 201


@application.delete("/cable")
def cable_delete():
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415
    device = request.get_json()
    link_id = device['data']['label']
    api_url = f"http://{gns_url}/v2/projects/{project_id}/links/{link_id}"
    requests.delete(api_url)
    return f"{link_id} was deleted", 201

@application.patch("/device")
def device_update():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415
    update = request.get_json()

    # Only re-apply the config if the status is set to staged
    if update['data']['status']['value'] == 'staged':
        # POST request to render config from NetBox
        api_url = f"http://gns3.domski.tech:8000/api/dcim/devices/{update['data']['id']}/render-config/"
        response = requests.post(api_url, headers={'authorization' : f'Token {netbox_token}'})
        
        # Push config using NAPALM to device
        mgmt_ip = ipaddress.IPv4Interface(update['data']['primary_ip4']['address']).ip
        device_driver = get_network_driver("iosxr")
        device = device_driver(hostname=mgmt_ip,username='drusso',password='cisco!123')
        device.open()
        device.load_replace_candidate(config=response.json()['content'])
        device.commit_config()
        device.close()
        nb.dcim.devices.update([{'id': update['data']['id'], 'status': "active"}])

    # Happy return code back to netbox
    return f"{update['data']['name']} is being configured", 201

# Debug
@application.post("/debug")
def debug():
    print("Uh Oh") # Sarcastic remark

    print(json.dumps(request.get_json(),indent=4))
    return "debug'd!", 201