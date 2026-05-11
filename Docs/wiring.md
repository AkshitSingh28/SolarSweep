# GPIO Wiring Reference

## Raspberry Pi Zero 2W — BCM Pin Map

| Signal               | BCM GPIO | Physical Pin |
|----------------------|----------|--------------|
| Drive Motor IN1      | 17       | 11           |
| Drive Motor IN2      | 27       | 13           |
| Drive Motor EN (PWM) | 22       | 15           |
| Lift Motor IN1       | 23       | 16           |
| Lift Motor IN2       | 24       | 18           |
| Lift Motor EN (PWM)  | 25       | 22           |
| Brush Motor IN1      | 5        | 29           |
| Brush Motor IN2      | 6        | 31           |
| Brush Motor EN (PWM) | 13       | 33           |
| Water Pump Relay     | 16       | 36           |
| Aux Relay            | 20       | 38           |
| Limit Front          | 18       | 12           |
| Limit Rear           | 19       | 35           |
| Limit Top            | 21       | 40           |
| Limit Bottom         | 26       | 37           |
| Rain Sensor          | 12       | 32           |
| IR Obstacle          | 4        | 7            |
| MCP3008 SPI MOSI     | 10       | 19           |
| MCP3008 SPI MISO     | 9        | 21           |
| MCP3008 SPI CLK      | 11       | 23           |
| MCP3008 SPI CS       | 8        | 24           |

## Motor Driver (L298N)
- IN1/IN2 control direction
- EN pin: PWM for speed control
- Connect motor supply (12V) to VS, logic supply (5V) to VSS

## Relay Module
- Active LOW: GPIO HIGH = relay OFF, GPIO LOW = relay ON
- Relay 1: Water pump
- Relay 2: Auxiliary (future use)

## Limit Switches
- Wired as normally open (NO)
- Pull-up resistors enabled in software (GPIO.PUD_UP)
- Active LOW when triggered
