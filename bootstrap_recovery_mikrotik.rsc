# =====================================================================
# RBRCS Bootstrap Auto-Recovery Script for MikroTik RouterOS
# 
# Purpose: This script enables 100% automated recovery for a router
# after a complete factory reset or hardware replacement.
#
# Instructions:
# 1. Edit the CONFIGURATION VARIABLES below to match your network.
# 2. Upload this file to your factory-reset MikroTik router.
# 3. Open the router terminal and run:
#    /import file-name=bootstrap_recovery_mikrotik.rsc
# =====================================================================

# --- CONFIGURATION VARIABLES ---
# Update these to match your RBRCS Server and the target Router
:global serverIP "192.168.1.100"
:global serverPort "8080"
:global routerID "router-main"
:global staticIP "192.168.1.1/24"
:global iface "ether1"
:global adminPass "ChangeMe@2024!"

# 1. Set static IP so the router can reach the RBRCS network
/ip address add address=$staticIP interface=$iface disabled=no

# 2. Enable SSH (port 22) and set a secure admin password
/ip service set ssh disabled=no port=22
/user set admin password=$adminPass

# 3. Create a delayed auto-recovery job
# This fetches the latest backup from the RBRCS server every 5 minutes and imports it.
/system scheduler add name="rbrcs-auto-recovery" interval=5m on-event="/tool fetch url=\"http://$serverIP:$serverPort/api/get-config/$routerID\" mode=http dst-path=recovery.rsc; /import file-name=recovery.rsc"

:put "=========================================================="
:put " RBRCS Bootstrap loaded successfully!"
:put " The router will now attempt auto-recovery every 5 minutes."
:put " Check your RBRCS dashboard events to monitor progress."
:put "=========================================================="
