import RPi.GPIO as GPIO
import time
import subprocess
import threading
import requests
import json
import socket
from pathlib import Path

# --- OLED IMPORTS ---
import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# --- CONFIG ---
# !!! UPDATE THESE VARIABLES !!!
DISCORD_WEBHOOK_URL = 'https://discordapp.com/api/webhooks/1424340105317060623/o42AvQgBy8FC5b6espYW6pMeaftJWNW4zx9GMttOkAyyJmHWEp_YNuvQhMQfuJMDyJCQ' 
RCLONE_REMOTE = 'camsetup:pi-cam' 
CLIP_SECONDS = 20
JPEG_QUALITY = 50
VIDEO_BITRATE = 2000000

# STREAMING CONFIG (Must match your Pi's IP address)
STREAM_PORT = 5000 

# --- PIN MAPPING (Based on Final Wiring) ---
# Inputs
PIR_A_PIN = 17      # PIR used for Ultrasonic confirmation (Logic 1)
PIR_B_PIN = 27      # PIR used for Laser/RCWL confirmation (Logic 2) 
LASER_RX_PIN = 22   # Single Laser Receiver Pin
RCWL_PIN = 4        # RCWL Doppler Radar Input (Logic 2)
TRIG_PIN = 23       # HC-SR04 Trigger
ECHO_PIN = 24       # HC-SR04 Echo

# Outputs
BUZZER_PIN = 18
ARM_LED = 26        # ARM_LED moved to BCM 26
LASER_TX_PIN = 5    # HW-493 Laser Transmitter Control

# OLED Configuration
OLED_WIDTH = 128
OLED_HEIGHT = 64
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE_SMALL = 10
FONT_SIZE_ALERT = 14

# --- GLOBAL STATE ---
trigger_lock = threading.Lock()
stream_process = None # Holds the continuous stream process
last_trigger_time = 0
debounce_interval = 10
ultrasonic_triggered = False 
oled = None
oled_thread_active = True


# --- PATHS ---
MEDIA_DIR = Path('/home/pi/videos')
PHOTO_DIR = MEDIA_DIR / 'photos'
VIDEO_DIR = MEDIA_DIR / 'clips'
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)


# --- HELPERS ---

def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "No IP"

def set_arm_led(state):
    """Controls the ARM status LED and Laser TX module."""
    GPIO.output(ARM_LED, state)
    GPIO.output(LASER_TX_PIN, state) # Turn Laser ON/OFF with ARM LED

def activate_buzzer(duration=0.5, cycles=3):
    """Blinks the buzzer for critical alerts."""
    try:
        for _ in range(cycles):
            GPIO.output(BUZZER_PIN, GPIO.HIGH)
            time.sleep(duration / (2 * cycles))
            GPIO.output(BUZZER_PIN, GPIO.LOW)
            time.sleep(duration / (2 * cycles))
    except Exception as e:
        print(f"Buzzer error: {e}")

# --- OLED FUNCTIONS ---

def oled_thread_task():
    """Constantly updates the OLED with system status and IP."""
    global oled_thread_active
    ip_address = get_ip_address()
    
    try:
        font_small = ImageFont.truetype(FONT_PATH, FONT_SIZE_SMALL)
    except IOError:
        font_small = ImageFont.load_default()

    while oled_thread_active:
        if not oled:
            time.sleep(1)
            continue
            
        try:
            image = Image.new('1', (OLED_WIDTH, OLED_HEIGHT), 0)
            draw = ImageDraw.Draw(image)
            
            # Status
            draw.text((0, 0), "SYSTEM ARMED", font=font_small, fill=255)
            draw.text((0, 12), "--------------------", font=font_small, fill=255)
            
            # Logic Status (Sensor states)
            draw.text((0, 28), f"L1 (PIR A): {GPIO.input(PIR_A_PIN)}", font=font_small, fill=255)
            draw.text((0, 38), f"L2 (TRIPLE): {GPIO.input(PIR_B_PIN)} {GPIO.input(RCWL_PIN)} {GPIO.input(LASER_RX_PIN)}", font=font_small, fill=255)
            
            # IP Address
            draw.text((0, 52), f"IP: {ip_address}", font=font_small, fill=255)

            oled.image(image)
            oled.show()
            time.sleep(1) # Update every 1 second
            
        except Exception as e:
            print(f"OLED background update error: {e}")
            time.sleep(5)


def display_alert(message, duration=5):
    """Displays a blinking, bold alert on the OLED, overriding the background thread."""
    global oled_thread_active
    
    # Temporarily pause background updates
    oled_thread_active = False 
    
    try:
        font_alert = ImageFont.truetype(FONT_PATH, FONT_SIZE_ALERT)
    except IOError:
        font_alert = ImageFont.load_default()
        
    start_time = time.time()
    
    while (time.time() - start_time) < duration:
        if not oled:
            break
            
        for fill_state in [255, 0]: # Blink on/off
            image = Image.new('1', (OLED_WIDTH, OLED_HEIGHT), 0)
            draw = ImageDraw.Draw(image)
            
            # Center the text (rough estimate for 128x64)
            draw.text((0, 20), message, font=font_alert, fill=fill_state)
            
            oled.image(image)
            oled.show()
            time.sleep(0.3)
            if (time.time() - start_time) >= duration:
                break

    # Clear and resume background updates
    oled.fill(0)
    oled.show()
    oled_thread_active = True


# --- STREAMING & CAMERA FUNCTIONS ---

def start_live_stream():
    """Starts the continuous video stream over LAN using rpicam-vid and a TCP socket."""
    global stream_process
    
    if stream_process is None:
        print(f"Starting continuous stream on port {STREAM_PORT}...")
        try:
            # rpicam-vid command to capture video and output to a TCP socket
            stream_command = [
                'rpicam-vid', '-t', '0', '--inline', '-n', 
                '--listen', '-o', 'tcp://0.0.0.0:' + str(STREAM_PORT), 
                '--width', '640', '--height', '480'
            ]
            
            stream_process = subprocess.Popen(stream_command)
            
            print(f"Stream running. View via VLC: tcp://<Pi_IP_Address>:{STREAM_PORT}")
            
        except Exception as e:
            print(f"Error starting stream: {e}")

def stop_live_stream():
    """Stops the continuous stream process."""
    global stream_process
    if stream_process:
        print("Stopping continuous stream...")
        stream_process.terminate()
        stream_process.wait()
        stream_process = None
        print("Stream stopped.")


def capture_photo(path: Path):
    """Captures a still image using rpicam-still."""
    print(f"Capturing photo to: {path}")
    try:
        subprocess.run(['rpicam-still', '-n', '-o', str(path), '--quality', str(JPEG_QUALITY)], check=True)
        print("Capture complete.")
        return True
    except Exception as e:
        print(f'Capture photo error: {e}')
        return False

def capture_video(path: Path):
    """Captures a video clip using rpicam-vid."""
    print(f"Recording video to: {path}")
    try:
        subprocess.run([
            'rpicam-vid', 
            '-t', str(CLIP_SECONDS * 1000), 
            '-o', str(path), 
            '--bitrate', str(VIDEO_BITRATE),
            '--hflip', '--vflip', '-n' # Add hflip/vflip if needed
        ], check=True)
        print("Video recording complete.")
        return True
    except Exception as e:
        print(f'Video recording error: {e}')
        return False

def upload_with_rclone(file_path: Path):
    """Uploads a file to Google Drive using the rclone subprocess."""
    print(f"Starting rclone upload of: {file_path.name}")
    try:
        subprocess.run(['rclone', 'copy', str(file_path), RCLONE_REMOTE], check=True)
        print(f"Upload Done for {file_path.name}.")
        # Optional: Delete local file after successful upload
        # file_path.unlink()
        return True
    except subprocess.CalledProcessError as e:
        print(f'Rclone upload failed: {e}')
        return False
    except Exception as e:
        print(f'General upload error: {e}')
        return False

def send_webhook_alert(content, file_path=None):
    """Sends a message or file to the Discord webhook."""
    if DISCORD_WEBHOOK_URL == 'YOUR_ACTUAL_DISCORD_WEBHOOK_URL_HERE':
        print("Webhook alert skipped: URL not set.")
        return

    payload = {'content': content}
    files = {}
    
    if file_path and Path(file_path).exists():
        file_name = Path(file_path).name
        files['file'] = (file_name, open(file_path, 'rb'))
    
    try:
        print(f"Sending alert: {content}")
        if files:
            response = requests.post(DISCORD_WEBHOOK_URL, data=payload, files=files, timeout=30)
        else:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        
        response.raise_for_status()
        print(f"Discord alert sent successfully. Status: {response.status_code}")
        if files:
            files['file'][1].close()

    except requests.exceptions.RequestException as e:
        print(f"Discord webhook error: {e}")
        if files:
            files['file'][1].close()


# --- TRIGGER HANDLER ---

def handle_trigger(source, record_video=False, upload=False):
    global last_trigger_time

    with trigger_lock:
        current_time = time.time()
        if current_time - last_trigger_time < debounce_interval:
            print(f"Debounce active. Ignoring trigger from {source}.")
            return

        last_trigger_time = current_time
        print(f"!!! ALERT !!! Trigger received from {source}. Actions starting...")
        
        # 1. Capture snapshot immediately
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        photo_path = PHOTO_DIR / f'snapshot_{timestamp}.jpg'
        
        if capture_photo(photo_path):
            alert_text = f"ðŸš¨ {source} detected! Snapshot attached."
            send_webhook_alert(alert_text, file_path=photo_path)
            
            # 2. Handle video and upload if required (Logic 2)
            if record_video:
                activate_buzzer(duration=1.5, cycles=5)
                video_path = VIDEO_DIR / f'clip_{timestamp}.mp4'
                
                # Display critical alert on OLED
                display_alert("INTRUDER!", CLIP_SECONDS + 5)
                
                if capture_video(video_path):
                    send_webhook_alert(f"ðŸ“½ï¸ Video clip ({CLIP_SECONDS}s) recorded.", file_path=video_path)
                    
                    if upload:
                        # Upload both photo and video in separate threads
                        threading.Thread(target=upload_with_rclone, args=(photo_path,), daemon=True).start()
                        threading.Thread(target=upload_with_rclone, args=(video_path,), daemon=True).start()
                else:
                    send_webhook_alert("âŒ Video recording FAILED.")

# --- SENSOR CALLBACKS ---

def get_distance():
    """Measures distance using the HC-SR04 ultrasonic sensor."""
    GPIO.output(TRIG_PIN, GPIO.LOW)
    time.sleep(0.000002)
    GPIO.output(TRIG_PIN, GPIO.HIGH)
    time.sleep(0.000010)
    GPIO.output(TRIG_PIN, GPIO.LOW)

    pulse_start = time.time()
    pulse_end = time.time()
    
    # Wait for ECHO to go HIGH (Start of pulse)
    while GPIO.input(ECHO_PIN) == GPIO.LOW:
        pulse_start = time.time()
        if pulse_start - pulse_end > 0.1: # Timeout
            return 999 

    # Wait for ECHO to go LOW (End of pulse)
    while GPIO.input(ECHO_PIN) == GPIO.HIGH:
        pulse_end = time.time()
        if pulse_end - pulse_start > 0.1: # Timeout
            return 999 

    duration = pulse_end - pulse_start
    # Speed of sound is 34300 cm/s
    distance = duration * 17150 # duration * (34300 / 2)
    
    return round(distance / 100, 2) # Return in meters

def ultrasonic_check():
    """Puts the ultrasonic_triggered flag up if distance is < 3m."""
    global ultrasonic_triggered
    try:
        distance = get_distance()
        if distance < 3.0 and distance > 0: # Trigger if closer than 3 meters
            ultrasonic_triggered = True
        else:
            ultrasonic_triggered = False
    except Exception as e:
        print(f"Ultrasonic error: {e}")
        ultrasonic_triggered = False


# LOGIC 1: Ultrasonic (<3m) + PIR A (17) -> Snapshot & Notify
def pir_a_cb(channel):
    """Logic 1: PIR A confirms Ultrasonic reading."""
    global ultrasonic_triggered
    # Check if PIR A is rising AND the ultrasonic flag is currently TRUE
    if GPIO.input(PIR_A_PIN) == GPIO.HIGH and ultrasonic_triggered:
        print("LOGIC 1 Triggered: Ultrasonic + PIR A CONFIRMED")
        display_alert("MOTION DETECTED", 3)
        handle_trigger('USONIC_PIR_A_CONFIRM', record_video=False, upload=False)
        ultrasonic_triggered = False # Reset flag

# LOGIC 2: PIR B (27) + Laser Break (22) + RCWL (4) -> Record, Upload, Stream
def high_security_cb(channel):
    """Logic 2: Triple confirmation check."""
    # This function is triggered ONLY by the Laser beam break (FALLING edge)
    if channel == LASER_RX_PIN and GPIO.input(LASER_RX_PIN) == 0: 
        
        # Check the state of the two other sensors (PIR B and RCWL)
        pir_b_state = GPIO.input(PIR_B_PIN) 
        rcwl_state = GPIO.input(RCWL_PIN) 

        # All three must be TRUE (Laser break (0), PIR B (1), and RCWL (1))
        if pir_b_state and rcwl_state:
            print("LOGIC 2 Triggered: PIR B + Laser + RCWL CONFIRMED")
            # Stream is running continuously, so we only handle the recording/alerts
            handle_trigger('TRIPLE_SECURITY_TRIP', record_video=True, upload=True)
        else:
            print(f"Triple check failed: Laser ok, but PIR B={pir_b_state}, RCWL={rcwl_state}")


# --- MAIN ---

def main():
    global oled
    
    try:
        # 1. HARDWARE SETUP
        GPIO.setmode(GPIO.BCM)
        # Inputs
        GPIO.setup(PIR_A_PIN, GPIO.IN)
        GPIO.setup(PIR_B_PIN, GPIO.IN)
        GPIO.setup(RCWL_PIN, GPIO.IN)
        GPIO.setup(LASER_RX_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP) # Receiver
        GPIO.setup(ECHO_PIN, GPIO.IN)
        # Outputs
        GPIO.setup(TRIG_PIN, GPIO.OUT)
        GPIO.setup(BUZZER_PIN, GPIO.OUT)
        GPIO.setup(ARM_LED, GPIO.OUT)
        GPIO.setup(LASER_TX_PIN, GPIO.OUT) # Transmitter

        # Initial States (System Armed)
        set_arm_led(GPIO.HIGH) # Turns LED and Laser TX ON
        GPIO.output(BUZZER_PIN, GPIO.LOW)
        GPIO.output(TRIG_PIN, GPIO.LOW) # Ensure TRIG is low initially
        
        # 2. OLED SETUP
        i2c = busio.I2C(board.SCL, board.SDA)
        oled = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c)
        oled.fill(0)
        oled.show()
        
        # 3. START CONTINUOUS LIVE STREAM & OLED THREADS
        start_live_stream()
        threading.Thread(target=oled_thread_task, daemon=True).start()

        # 4. EVENT DETECTION
  GPIO.add_event_detect(PIR_A_PIN, GPIO.RISING, bouncetime=800)
      GPIO.add_event_callback(PIR_A_PIN, pir_a_cb)

      GPIO.add_event_detect(LASER_RX_PIN, GPIO.FALLING, bouncetime=400) 
       GPIO.add_event_callback(LASER_RX_PIN, high_security_cb)

        # 5. ULTRASONIC LOOP
        while True:
            # Check ultrasonic distance every 0.5s
            ultrasonic_check()
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("Exiting program.")
    except Exception as e:
        print(f"An unhandled error occurred: {e}")
    finally:
        global oled_thread_active
        oled_thread_active = False # Stop OLED thread
        if oled:
            oled.fill(0)
            oled.show()
        stop_live_stream() # Stop the stream
        set_arm_led(GPIO.LOW) # Turn off LED and Laser TX
        GPIO.cleanup()

if __name__ == '__main__':
    main()
