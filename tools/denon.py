import argparse
import socket
import sys

# MASTER VOLUME = +1.0dB :  MV81<CR>
#                 +0.5dB :  MV805<CR>
#                    0dB :  MV80<CR>
#                 -0.5dB :  MV795<CR>
#                 -1.0dB :  MV79<CR>
#                 -1.0dB :  MV79<CR>
#                    |        |
#                -79.5dB :  MV005<CR>
#                -80.0dB :  MV00<CR>
#                   MUTE :  MV99<CR>

# map inputs by name
input_map = {
    'AppleTV': {'cmd': 'SIMPLAY'},
    'TiVo': {'cmd': 'SISAT/CBL'},
    'PS4': {'cmd': 'SIGAME'},
    'Plex': {'cmd': 'SIBD'},
    'Vinyl': {'cmd': 'SICD'},
    'Network': {'cmd': 'SINET'}
}

denon_ip = '192.168.1.116'

# name = data.get('name', 'world')
# logger.info("Hello {}".format(name))
# hass.bus.fire(name, { "wow": "from a Python script!" })



def denon_api(command, return_match=None, debug=False):
    ''' just a telnet interface '''
    if return_match is None:
        return_match = command.split(" ")[0]

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((denon_ip, 23))
    s.sendall("{}\n".format(command).encode())
    while True:
        data = s.recv(135).split('\r'.encode()) # denon spec says 135 bytes max
        for line in data:
            # if debug:
            #     logger.info("Received: {}".format(line.decode()))
            if line.strip().startswith(return_match.encode()):
                s.shutdown(socket.SHUT_WR)
                s.close()
                return(line.decode())
    s.shutdown(socket.SHUT_WR)
    s.close()
    return "Connection closed."


def power_state(string=True):
    denon_power = denon_api(command="PW?", return_match='PW')
    if denon_power == "PWON":
        if string:
            return "on"
        else:
            return True
    else:
        if string:
            return "off"
        else:
            return False


def power_on():
    ''' you turn me on baby '''
    if not power_state(string=False):
        denon_api(command="PWON", return_match='PW')


def power_off():
    ''' GTFO '''
    if power_state(string=False):
        denon_api(command="PWSTANDBY", return_match='PW')


def select_input(source, state_check=False):
    # set Denon to requested source
    state_matches = True
    denon_input = denon_api(command="SI?", return_match='SI')
    if denon_input != source:
        state_matches = False
        if not state_check:
            denon_api(command=source, return_match='SI')
    return state_matches


def dyn_eq_state():
    dyn_eq = denon_api(command="PSDYNEQ ?", return_match='PSDYNEQ')
    if dyn_eq == "PSDYNEQ ON":
        if string:
            return "on"
        else:
            return True
    else:
        if string:
            return "off"
        else:
            return False


def dyn_eq_on():
    if not dyn_eq_state(string=False):
        denon_api(command="PSDYNEQ ON", return_match='PW')


def main():
    parser = argparse.ArgumentParser(description='Denon API')
    parser.add_argument('-c', '--command', help='command to send to Denon')
    parser.add_argument('-r', '--return_match', default=None, help='return output matching chars')
    args = parser.parse_args()

    returned = denon_api(command=args.command, return_match=args.return_match)

    print(returned)


if __name__ == "__main__":
    main()

