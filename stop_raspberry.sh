#!/bin/bash
sudo systemctl stop intesa-dashboard
sudo systemctl disable intesa-dashboard || true
echo "Intesa dashboard stoppad."
