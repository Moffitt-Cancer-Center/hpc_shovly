export NODES="$(printf 'hpctpa3pc%04d,' {1..70} | sed 's/,$//;')"
echo "Hostname,Cores,RAM_GB,GPU_Count,GPU_Model,CPU_Model" > cluster_inventory.csv
pdsh -w "$NODES" "$PWD/get_hardware.sh" | awk '{print $2}' >> cluster_inventory.csv
