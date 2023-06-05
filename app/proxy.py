import requests,pynetbox,random, json, threading, ipaddress
from flask import Flask, request, jsonify
from telnetlib import Telnet
from napalm import get_network_driver
from jinja2 import Template
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

    # Set variables accordingly
    id = device['data']['id']
    template_id = device['data']['device_type']['slug']
    name = device['data']['name']

    # Make API call to GNS3 to create the VM
    api_url = f"http://{gns_url}/v2/projects/{project_id}/templates/{template_id}"
    data = {"x": random.randrange(-800,800), "y": random.randrange(-500,500), "name": f"{name}", "compute_id": "local"}
    response = requests.post(api_url, json=data)

    # Extract GNS3 assigned data
    node_id = response.json()["node_id"]
    console = response.json()["console"]

    # Generate mac address for mgmt nic
    mac_address = "00:20:91:%02x:%02x:%02x" % (random.randint(0, 255),random.randint(0, 255),random.randint(0, 255))
    options = f"-nic bridge,br=br0,model=e1000,mac={mac_address}"

    # Make API call to update the VM's name and Mgmt nic in GNS3
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}"
    data = {"name": name, "properties": { "options": options } }
    response = requests.put(api_url, json=data)

    # Update netbox console port and node_id
    nb.dcim.devices.update([{'id': id, 'serial': node_id, 'asset_tag': console}])

    # Begin ZTP
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}/start"
    requests.post(api_url)

    # Allocate a mgmt ip address
    primary_ip4 = nb.ipam.prefixes.get(2).available_ips.create() # 2 is the Mgmt prefix

    # Find mgmt interface ID
    int_id = nb.dcim.interfaces.get(device_id=id,name="MgmtEth0/0/CPU0/0")['id']

    # Assign mgmt IP to the mgmt interface
    nb.ipam.ip_addresses.update([{'id': primary_ip4.id, 'vrf': 1, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': int_id}])

    # Set the mgmt interface vrf
    nb.dcim.interfaces.update([{'id': int_id, 'vrf': 1}])

    # set the status and primary ip of the device 
    nb.dcim.devices.update([{'id': id, 'status': "planned", 'primary_ip6': primary_ip4.id}])

    # Call the "ZTP" telnet script
    device_args=[console,name,primary_ip4,id,template_id]
    configure_thread = threading.Thread(target=configure, name="configure_device", args=device_args)
    configure_thread.start()

    # Happy return code back to netbox
    return f"Node {node_id} was created", 201

@application.delete("/device")
def device_delete():
    #Error checking
    if not request.is_json:
        return {"error": "Request must be JSON"}, 415
    
    # Find the device's node_id from the call's json body
    device = request.get_json()
    node_id = device['data']['serial']

    # Turn off th router
    requests.post(f"http://{gns_url}/v2/projects/{project_id}/nodes/{device['data']['serial']}/stop")
    name = device['data']['name']

    # Delete the router
    api_url = f"http://{gns_url}/v2/projects/{project_id}/nodes/{node_id}"
    requests.delete(api_url)

    # Happy return code back to netbox
    return f"{name} was deleted", 201

@application.post("/cable")
def cable():
    print("Yo Dawg, I heard you like cables?") # Sarcastic remark

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
    if device_a['device_role']['id'] == 3:
        # If a is a core..
        if device_b['device_role']['id'] == 3:
            # core to core, do is-is (tag 1 is is-is)
            nb.dcim.interfaces.update([{'id': cable['data']['a_terminations'][0]['object_id'], 'vrf': 2, 'tags': [{'id': 1}]}])
            nb.dcim.interfaces.update([{'id': cable['data']['b_terminations'][0]['object_id'], 'vrf': 2, 'tags': [{'id': 1}]}])

            # generate v6 prefix for link, 6 is the prefix to allocate /127s from. should be chosen dynamically
            prefix = nb.ipam.prefixes.get(6).available_prefixes.create({"prefix_length": 127})
            # generate ip addresses from prefix
            ip_a_side = prefix.available_ips.create()
            ip_b_side = prefix.available_ips.create()

            # push ip address changes to Netbox
            nb.ipam.ip_addresses.update([{'id': ip_a_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['a_terminations'][0]['object_id']}])
            nb.ipam.ip_addresses.update([{'id': ip_b_side.id, 'assigned_object_type': 'dcim.interface', 'assigned_object_id': cable['data']['b_terminations'][0]['object_id']}])

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
        mgmt_ip = ipaddress.IPv6Interface(update['data']['primary_ip6']['address']).ip
        device_driver = get_network_driver("iosxr")
        device = device_driver(hostname=mgmt_ip,username='cisco',password='cisco')
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

def configure(port,hostname,ip4,id,template_id):

    xrv_config = [
     (b"Press RETURN to get started",b"\r"),
     (b"Enter root-system username:",b"drusso\n", 60),
     (b":",b"cisco!123\n"),
     (b":",b"cisco!123\n"),
     (b"SYSTEM CONFIGURATION COMPLETED", b"\r", 120),
     (b"Username:", b"drusso\n", 120),
     (b"Password:", b"cisco!123\n"),
     (b"#", b"config\n"),
     (b"#", f"hostname {hostname}\n".encode('utf8')),
     (b"#", b"vrf Mgmt address-family ipv4 unicast\n"),
     (b"#", b"exit\n"),
     (b"#", b"exit\n"),
     (b"#", b"router static vrf Mgmt address-family ipv4 unicast 0.0.0.0/0 172.24.16.1\n"),
     (b"#", b"interface MgmtEth0/0/CPU0/0\n"),
     (b"#", b"vrf Mgmt\n"),
     (b"#", f"ipv4 address {ip4}\n".encode('utf8')),
     (b"#", b"no shut\n"),
     (b"#", b"exit\n"),
     (b"#", b"ssh server v2\n"),
     (b"#", b"ssh server vrf Mgmt\n"),
     (b"#", b"lldp\n"),
     (b"#", b"exit\n"),
     (b"#", b"xml agent tty iteration off\n"),
     (b"#", b"xml agent vrf Mgmt\n"),
     (b"#", b"end\n"),
     (b":", b"yes\n"),
     (b"#", b"crypto key generate rsa\n"),
     (b":", b"\n"),
     (b"#", b"exit\n")]
    
    xrv9k_config = [
     (b"Press RETURN to get started",b"\r"),
     (b"Enter root-system username:",b"drusso\n", 60),
     (b":",b"cisco!123\n"),
     (b":",b"cisco!123\n"),
     (b"SYSTEM CONFIGURATION COMPLETED", b"\r", 120),
     (b"Username:", b"drusso\n", 120),
     (b"Password:", b"cisco!123\n"),
     (b"#", b"config\n"),
     (b"#", f"hostname {hostname}\n".encode('utf8')),
     (b"#", b"vrf Mgmt address-family ipv4 unicast\n"),
     (b"#", b"exit\n"),
     (b"#", b"exit\n"),
     (b"#", b"router static vrf Mgmt address-family ipv4 unicast 0.0.0.0/0 172.24.16.1\n"),
     (b"#", b"interface MgmtEth0/0/CPU0/0\n"),
     (b"#", b"vrf Mgmt\n"),
     (b"#", f"ipv4 address {ip4}\n".encode('utf8')),
     (b"#", b"no shut\n"),
     (b"#", b"exit\n"),
     (b"#", b"ssh server v2\n"),
     (b"#", b"ssh server vrf Mgmt\n"),
     (b"#", b"lldp\n"),
     (b"#", b"exit\n"),
     (b"#", b"xml agent tty iteration off\n"),
     (b"#", b"xml agent vrf Mgmt\n"),
     (b"#", b"end\n"),
     (b":", b"yes\n"),
     (b"#", b"exit\n")]
    
    match template_id:
        case "":
            conf = xrv9k_config
        case "9a99c0d2-cc17-4452-9e74-57ba5cd166eb":
            conf = xrv_config
        case _:
            nb.dcim.devices.update([{'id': id, 'status': "failed"}])
            return "fail"
    
    tn = Telnet('gns3.domski.tech', port)
    for line in conf:
        if len(line) == 3:
            tn.read_until(line[0],timeout=line[2])
        else:
            tn.read_until(line[0])
        tn.write(line[1])
    nb.dcim.devices.update([{'id': id, 'status': "active"}])