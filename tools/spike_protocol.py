from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Union


# These constants come from the official SPIKE Prime RPC transport.
HIGH_PRIORITY_DELIMITER = 0x01
MESSAGE_DELIMITER = 0x02
ESCAPE_XOR = 0x03
COBS_CODE_OFFSET = 3
MAX_COBS_BLOCK_SIZE = 84

RPC_SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"
RPC_TX_CHAR_UUID = "00001624-1212-efde-1623-785feabcd123"
RPC_RX_CHAR_UUID = "00001625-1212-efde-1623-785feabcd123"


class ResponseStatus(IntEnum):
    ACK = 0
    NACK = 1


class ProgramAction(IntEnum):
    START = 0
    STOP = 1


class MessageType(IntEnum):
    INFO_REQUEST = 0x00
    INFO_RESPONSE = 0x01
    START_FILE_UPLOAD_REQUEST = 0x0C
    START_FILE_UPLOAD_RESPONSE = 0x0D
    TRANSFER_CHUNK_REQUEST = 0x10
    TRANSFER_CHUNK_RESPONSE = 0x11
    GET_HUB_NAME_REQUEST = 0x1C
    GET_HUB_NAME_RESPONSE = 0x1D
    PROGRAM_FLOW_REQUEST = 0x1E
    PROGRAM_FLOW_RESPONSE = 0x1F
    CLEAR_SLOT_REQUEST = 0x46
    CLEAR_SLOT_RESPONSE = 0x47
    DEVICE_UUID_REQUEST = 0x08
    DEVICE_UUID_RESPONSE = 0x09
    CONSOLE_NOTIFICATION = 0x21
    PROGRAM_FLOW_NOTIFICATION = 0x43


@dataclass(frozen=True)
class InfoResponse:
    rpc_version_major: int
    rpc_version_minor: int
    rpc_build: int
    firmware_version_major: int
    firmware_version_minor: int
    firmware_build: int
    max_packet_size: int
    max_message_size: int
    max_chunk_size: int
    product_variant: int
    payload: bytes

    @classmethod
    def parse(cls, payload: bytes) -> "InfoResponse":
        expected_size = struct.calcsize("<BBBHBBHHHHH")
        if len(payload) != expected_size:
            raise ValueError(f"InfoResponse must be {expected_size} bytes, got {len(payload)} bytes.")
        unpacked = struct.unpack("<BBBHBBHHHHH", payload)
        return cls(
            rpc_version_major=unpacked[1],
            rpc_version_minor=unpacked[2],
            rpc_build=unpacked[3],
            firmware_version_major=unpacked[4],
            firmware_version_minor=unpacked[5],
            firmware_build=unpacked[6],
            max_packet_size=unpacked[7],
            max_message_size=unpacked[8],
            max_chunk_size=unpacked[9],
            product_variant=unpacked[10],
            payload=payload,
        )


@dataclass(frozen=True)
class StatusResponse:
    message_type: MessageType
    status: ResponseStatus
    payload: bytes

    @property
    def ok(self) -> bool:
        return self.status == ResponseStatus.ACK


@dataclass(frozen=True)
class HubNameResponse:
    name: str
    payload: bytes


@dataclass(frozen=True)
class DeviceUuidResponse:
    uuid: bytes
    payload: bytes


@dataclass(frozen=True)
class ConsoleNotification:
    text: str
    payload: bytes


@dataclass(frozen=True)
class ProgramFlowNotification:
    status: int
    action: int
    slot: int
    payload: bytes


@dataclass(frozen=True)
class UnknownMessage:
    message_type: int
    payload: bytes


ParsedMessage = Union[
    InfoResponse,
    StatusResponse,
    HubNameResponse,
    DeviceUuidResponse,
    ConsoleNotification,
    ProgramFlowNotification,
    UnknownMessage,
]


def info_request() -> bytes:
    return bytes([MessageType.INFO_REQUEST])


def get_hub_name_request() -> bytes:
    return bytes([MessageType.GET_HUB_NAME_REQUEST])


def device_uuid_request() -> bytes:
    return bytes([MessageType.DEVICE_UUID_REQUEST])


def clear_slot_request(slot: int) -> bytes:
    return struct.pack("<BB", MessageType.CLEAR_SLOT_REQUEST, slot)


def start_file_upload_request(filename: str, slot: int, file_crc: int) -> bytes:
    filename_bytes = filename.encode("utf-8")
    if len(filename_bytes) > 31:
        raise ValueError("Hub upload filename must be 31 bytes or fewer.")
    padded_filename = filename_bytes + b"\x00" * (32 - len(filename_bytes))
    return struct.pack("<B32sBI", MessageType.START_FILE_UPLOAD_REQUEST, padded_filename, slot, file_crc)


def transfer_chunk_request(running_crc: int, chunk: bytes) -> bytes:
    return struct.pack("<BIH", MessageType.TRANSFER_CHUNK_REQUEST, running_crc, len(chunk)) + chunk


def program_flow_request(action: ProgramAction, slot: int) -> bytes:
    return struct.pack("<BBB", MessageType.PROGRAM_FLOW_REQUEST, action, slot)


def cobs_encode(data: bytes) -> bytes:
    encoded = bytearray()
    zero_index = 0
    search_index = 0

    while True:
        next_index = data.find(b"\x00", search_index)
        if next_index == -1:
            next_index = len(data)
        search_index = next_index + 1

        while next_index - zero_index > MAX_COBS_BLOCK_SIZE:
            encoded.append(COBS_CODE_OFFSET + MAX_COBS_BLOCK_SIZE)
            encoded.extend(data[zero_index : zero_index + MAX_COBS_BLOCK_SIZE])
            zero_index += MAX_COBS_BLOCK_SIZE

        encoded.append(COBS_CODE_OFFSET + next_index - zero_index)
        encoded.extend(data[zero_index:next_index])
        zero_index = next_index + 1

        if next_index == len(data):
            break

    return bytes(encoded)


def cobs_decode(data: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(data):
        offset = data[index] - COBS_CODE_OFFSET
        index += 1
        if offset < 0:
            raise ValueError("COBS block offset underflow.")
        decoded.extend(data[index : index + offset])
        index += offset
        if data[index - offset - 1] < COBS_CODE_OFFSET + MAX_COBS_BLOCK_SIZE and index < len(data):
            decoded.append(0)
    return bytes(decoded)


def pack_frame(message: bytes, *, high_priority: bool = False) -> bytes:
    encoded = bytes(byte ^ ESCAPE_XOR for byte in cobs_encode(message))
    prefix = bytes([HIGH_PRIORITY_DELIMITER]) if high_priority else b""
    return prefix + encoded + bytes([MESSAGE_DELIMITER])


def unpack_frame(frame: bytes) -> bytes:
    if not frame:
        raise ValueError("Cannot unpack an empty frame.")
    if frame[-1] != MESSAGE_DELIMITER:
        raise ValueError("Packed frames must end with the message delimiter.")
    if frame[0] == HIGH_PRIORITY_DELIMITER:
        frame = frame[1:]
    encoded = bytes(byte ^ ESCAPE_XOR for byte in frame[:-1])
    return cobs_decode(encoded)


def packetize_message(message: bytes, max_packet_size: int | None) -> list[bytes]:
    packed = pack_frame(message)
    if not max_packet_size or len(packed) <= max_packet_size:
        return [packed]
    return [packed[index : index + max_packet_size] for index in range(0, len(packed), max_packet_size)]


def crc32_update(data: bytes, seed: int = 0) -> int:
    # The official SPIKE Python example updates the CRC one chunk at a time.
    # Using zlib.crc32 incrementally matches that streaming behavior.
    return zlib.crc32(data, seed) & 0xFFFFFFFF


def crc32_for_file(data: bytes) -> int:
    padded = data + (b"\x00" * ((4 - (len(data) % 4)) % 4))
    return zlib.crc32(padded) & 0xFFFFFFFF


def decode_c_string(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def parse_message(payload: bytes) -> ParsedMessage:
    if not payload:
        raise ValueError("Cannot parse an empty SPIKE message payload.")

    message_type = payload[0]

    if message_type == MessageType.INFO_RESPONSE:
        return InfoResponse.parse(payload)

    if message_type in {
        MessageType.START_FILE_UPLOAD_RESPONSE,
        MessageType.TRANSFER_CHUNK_RESPONSE,
        MessageType.PROGRAM_FLOW_RESPONSE,
        MessageType.CLEAR_SLOT_RESPONSE,
    }:
        if len(payload) < 2:
            raise ValueError(f"Response {message_type:#x} is missing its status byte.")
        return StatusResponse(
            message_type=MessageType(message_type),
            status=ResponseStatus(payload[1]),
            payload=payload,
        )

    if message_type == MessageType.GET_HUB_NAME_RESPONSE:
        return HubNameResponse(name=decode_c_string(payload[1:]), payload=payload)

    if message_type == MessageType.DEVICE_UUID_RESPONSE:
        return DeviceUuidResponse(uuid=payload[1:], payload=payload)

    if message_type == MessageType.CONSOLE_NOTIFICATION:
        return ConsoleNotification(text=decode_c_string(payload[1:]), payload=payload)

    if message_type == MessageType.PROGRAM_FLOW_NOTIFICATION:
        if len(payload) < 4:
            raise ValueError("ProgramFlowNotification must include status, action, and slot.")
        return ProgramFlowNotification(
            status=payload[1],
            action=payload[2],
            slot=payload[3],
            payload=payload,
        )

    return UnknownMessage(message_type=message_type, payload=payload)
