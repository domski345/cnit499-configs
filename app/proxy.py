import requests,pynetbox,random, json, threading, ipaddress, time
from flask import Flask, request, jsonify, copy_current_request_context
from telnetlib import Telnet
from napalm import get_network_driver
from jinja2 import Template
from string import Template
application = Flask(__name__)
project_id = "6380036b-12e3-4870-b148-40446b369f23"
netbox_token = '61e2d3997197dd11e1963d5002fbf04ace859429'
gns_url = "gns3-test.domski.tech:3080"
nb = pynetbox.api('http://gns3-test.domski.tech:8000/', token=netbox_token)


@application.post("/device")
def device():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415

    # Get initial call from netbox webhook when device is created
    device = request.get_json()


    # Make API call to GNS3 to create the VM
    api_url = f"http://{gns_url}/v2/projects/{project_id}/templates/{nb.dcim.device_types.get(id=device['data']['device_type']['id'])['slug']}"
    data = {"x": random.randrange(-800,800), "y": random.randrange(-500,500), "name": f"{device['data']['name']}", "compute_id": "local"}
    response = requests.post(api_url, json=data)
    if not response.ok:
        print("Reason: "+ response.reason)
        print("Response: "+ response.text)
        return "", 500
    node_id = response.json()["node_id"]
    console = response.json()["console"]
    # Update netbox console port and node_id
    nb.dcim.devices.update([{'id': device['data']['id'], 'serial': node_id, 'custom_fields': {'console': console}, 'status': "planned"}])
    
    # Create device in another thread
    threading.Thread(target=create_device, name="configure_device", args=(node_id,console), kwargs=device).start()

    # Happy return code back to netbox
    return "Node was created", 201

def create_device(*args, **device):

    # Set variables accordingly
    id = device['data']['id']
    name = device['data']['name']
    device_type = nb.dcim.device_types.get(id=device['data']['device_type']['id'])
    conf = device_type['custom_fields']['ztp_config']

    # Extract GNS3 assigned data
    node_id = args[0]
    console = args[1]

    # Generate mac address for mgmt nic
    r1 = random.randint(0, 255)
    r2 = random.randint(0, 255)
    r3 = random.randint(0, 255)
    mac_address = "00:20:91:%02x:%02x:%02x" % (r1,r2,r3)
    is_is_id = "0020.91%02x.%02x%02x" % (r1,r2,r3)
    option = device_type['custom_fields']['options']
    options = f"-nic bridge,br=br0,model=e1000,mac={mac_address} {option or ''}"

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
    int_id = nb.dcim.interfaces.get(device_id=id,label=-2)['id']

    # Set the mgmt interface vrf and mac address
    nb.dcim.interfaces.update([{'id': int_id, 'vrf': 1, 'mac_address': mac_address}])

    # Assign mgmt IP to the mgmt interface
    nb.ipam.ip_addresses.update([{'id': primary_ip4.id, 'vrf': 1, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': int_id}])

    # Update netbox console port and node_id
    nb.dcim.devices.update([{'id': id, 'custom_fields': {'is_is_system_id': is_is_id}, 'primary_ip4': primary_ip4.id}])
    
    # Connect with telnet and begin configuring
    tn = Telnet('gns3-test.domski.tech', console)
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
    if not response.ok:
        print("Reason: "+ response.reason)
        print("Response: "+ response.text)
        return "", 500

    # Update netbox with the cable ID
    nb.dcim.cables.update([{'id': cable['data']['id'], 'label': response.json()["link_id"]}])
    
    role_a = device_a['device_role']['id']
    role_b = device_b['device_role']['id']
    # Control logic for determining the link type
    if role_a == 1 or role_a == 2:
        # If a is a core..
        if role_b == 1 or role_b == 2:
            # core to core, do is-is (tag 1 is is-is)
            nb.dcim.interfaces.update([{'id': cable['data']['a_terminations'][0]['object_id'], 'tags': [{'id': 1}]}])
            nb.dcim.interfaces.update([{'id': cable['data']['b_terminations'][0]['object_id'], 'tags': [{'id': 1}]}])

            # generate v6 prefix for link, 6 is the prefix to allocate /127s from. should be chosen dynamically
            prefix_v4 = nb.ipam.prefixes.get(5).available_prefixes.create({"prefix_length": 30})
            prefix_v6 = nb.ipam.prefixes.get(4).available_prefixes.create({"prefix_length": 127})
            # generate ip addresses from prefix
            ipv4_a_side = prefix_v4.available_ips.create()
            ipv4_b_side = prefix_v4.available_ips.create()
            ipv6_a_side = prefix_v6.available_ips.create()
            ipv6_b_side = prefix_v6.available_ips.create()

            # push ip address changes to Netbox
            nb.ipam.ip_addresses.update([{'id': ipv4_a_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['a_terminations'][0]['object_id']}])
            nb.ipam.ip_addresses.update([{'id': ipv4_b_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['b_terminations'][0]['object_id']}])
            nb.ipam.ip_addresses.update([{'id': ipv6_a_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['a_terminations'][0]['object_id']}])
            nb.ipam.ip_addresses.update([{'id': ipv6_b_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['b_terminations'][0]['object_id']}])

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
        api_url = f"http://gns3-test.domski.tech:8000/api/dcim/devices/{update['data']['id']}/render-config/"
        response = requests.post(api_url, headers={'authorization' : f'Token {netbox_token}'})
        if not response.ok:
            print("Reason: "+ response.reason)
            print("Response: "+ response.text)
            return "", 500
        
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

# Create new site
@application.post("/site")
def create_site():
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415
    update = request.get_json()
    slug = update['data']['slug']
    p1 = nb.dcim.devices.create(name=f"{slug}-p1",role=2,site=update['data']['id'],device_type=1)
    p2 = nb.dcim.devices.create(name=f"{slug}-p2",role=2,site=update['data']['id'],device_type=1)
    pe1 = nb.dcim.devices.create(name=f"{slug}-pe1",role=1,site=update['data']['id'],device_type=1)
    pe2 = nb.dcim.devices.create(name=f"{slug}-pe2",role=1,site=update['data']['id'],device_type=1)
    # Define connection interfaces 
    list = [
        (pe1,0,p1,2),
        (pe1,1,p2,2),
        (pe2,0,p1,3),
        (pe2,1,p2,3),
        (p1,0,p2,0),
        (p1,1,p2,1)
    ]
    for con in list:
        a = [{'object_id': nb.dcim.interfaces.get(device_id=con[0].id,label=con[1]).id, 'object_type': 'dcim.interface'}]
        b = [{'object_id': nb.dcim.interfaces.get(device_id=con[2].id,label=con[3]).id, 'object_type': 'dcim.interface'}]
        nb.dcim.cables.create(a_terminations=a,b_terminations=b)


    return f"{update['data']['display']} is being configured", 201

# Debug
@application.post("/debug")
def debug():
    print("Uh Oh") # Sarcastic remark

    print(json.dumps(request.get_json(),indent=4))
    return "debug'd!", 201