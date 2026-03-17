# IoT-Based Automated Hydroponic Tower for Monitoring and Control

An IoT-based software platform for automated monitoring and control of a hydroponic tower using simulated sensors and actuators, MQTT communication, REST services, and ThingsBoard integration.

## Overview

This project was developed to automate the monitoring and control of a hydroponic tower through a microservices-based IoT architecture. The system simulates environmental and hydroponic sensing, applies rule-based control logic, and provides real-time visibility through dashboards and cloud telemetry.

## Key Features

- Real-time monitoring of pH, EC, water level, air temperature, air humidity, water temperature, and light intensity
- Automated control of irrigation, nutrient dosing, pH adjustment, lighting, water refill, and environmental conditions
- MQTT-based communication between services
- REST-based configuration and coordination
- Real-time dashboard and alert support
- Cloud telemetry visualization with ThingsBoard

## System Architecture

The platform is organized into the following components:

- Device Connectors for simulated sensors and actuators
- MQTT Broker for asynchronous communication
- Tower Catalog for configuration and system metadata
- Control Services for automation logic
- Dashboard and Notification Layer for user awareness
- ThingsBoard Integration for telemetry storage and visualization

## Technologies Used

- Python
- MQTT
- REST APIs
- Mosquitto
- ThingsBoard
- Microservices Architecture

## Repository Structure

.
├── actuators/
├── catalog/
├── cloud/
├── controller/
├── logger/
├── sensors/
├── dashboard.py
├── runner.py
├── mosquitto.conf
└── README.md

## Team

This project was developed collaboratively by:

- Rouyan Mohtashami
- Amirhossein Ghaderi

## Contributions

Both authors contributed to the design, development, integration, and testing of the system.
