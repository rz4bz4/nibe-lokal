import sys

p = 'nibelokal/config.py'
s = open(p).read()

if '"lan_only"' in s:
    print('lan_only fanns redan i config.py')
    sys.exit(0)

# Hitta allowed_hosts-raden inne i DEFAULTS och lagg lan_only direkt efter.
needle = None
for line in s.splitlines():
    t = line.strip()
    if t.startswith('"allowed_hosts"') and t.endswith(','):
        needle = line
        break
if needle is None:
    sys.exit('hittade ingen allowed_hosts-rad i DEFAULTS')
if s.count(needle) != 1:
    sys.exit('allowed_hosts-raden ar inte unik')

add = (needle + '\n'
       '    # Kall-IP-mur. Mellanrumsseparerade CIDR-nat som far svar; allt\n'
       '    # annat far anslutningen stangd innan en HTTP-rad har lasts.\n'
       '    # Loopback slapps alltid igenom. Tom strang = ingen mur.\n'
       '    "lan_only": "",')
open(p, 'w').write(s.replace(needle, add))
print('config.py patchad: lan_only tillagd i DEFAULTS')
