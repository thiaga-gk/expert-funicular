# AWS EC2 Price Registry

`aws_ec2_on_demand.json` is the versioned Phase-1 fallback registry used to model customer AWS infrastructure cost for Pro/Classic SQL Warehouses when authoritative CUR/Data Export evidence is unavailable.

Realizer must label these values as **modeled economic cost**, not authoritative cash-realized AWS savings. Replace or augment this registry only through a versioned source/evidence change; never introduce a hidden region or rate fallback.
