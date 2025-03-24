import os
import base64
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from typing import Generator

class Encrypter:
    def __init__(
        self,
        *,
        key: bytes,
        iv: bytes=None,
        chunk_size: int=1024
    ):
        self.key = key
        self.iv = iv
        self.chunk_size = chunk_size
    
    def cipher(self, iv: bytes=None) -> Cipher:
        return Cipher(
            algorithms.AES(self.key), 
            modes.CTR(iv or self.iv)
        )
    
    def encrypt_file(
        self, 
        *, 
        src_file: str, 
        dest_file: str, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> None:
        self._crypt_file(
            src_file=src_file,
            dest_file=dest_file,
            chunk_size=chunk_size,
            cipher=self.cipher(iv).encryptor()
        )

    def decrypt_file(
        self, 
        *, 
        src_file: str, 
        dest_file: str, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> None:
        self._crypt_file(
            src_file=src_file,
            dest_file=dest_file,
            chunk_size=chunk_size,
            cipher=self.cipher(iv).decryptor()
        )

    def _crypt_file(
        self, 
        *, 
        src_file: str, 
        dest_file: str, 
        chunk_size: int,
        cipher: Cipher
    ) -> None:
        if not chunk_size:
            chunk_size = self.chunk_size
        
        with open(src_file, 'rb') as infile, open(dest_file, 'wb') as outfile:
            while chunk := infile.read(chunk_size):
                outfile.write(cipher.update(chunk))
            outfile.write(cipher.finalize())

    def encrypt_chunks(
        self, 
        data: str | bytes, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> Generator[bytes, None, None]:
        return self._encdec(
            data, 
            chunk_size, 
            self.cipher(iv).encryptor()
        )

    def encrypt(
        self, 
        data: str | bytes, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> bytes:
        return b''.join(list(self.encrypt_chunks(data, iv=iv, chunk_size=chunk_size)))

    def encrypt_b32(self, data: str, iv: bytes=None) -> str:
        return base64.b32encode(
            self.encrypt(data.encode('utf-8'), iv)
        ).decode('utf-8')

    def decrypt_chunks(
        self, 
        data: str | bytes, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> Generator[bytes, None, None]:
        return self._encdec(
            data, 
            chunk_size, 
            self.cipher(iv).decryptor()
        )

    def decrypt(
        self, 
        data: str | bytes, 
        iv: bytes=None,
        chunk_size: int | None=None
    ) -> bytes:
        return b''.join(list(self.decrypt_chunks(data, iv=iv, chunk_size=chunk_size)))

    def _encdec(
        self, 
        data: str | bytes, 
        chunk_size: int,
        cipher_context: Cipher
    ) -> Generator[bytes, None, None]:
        if isinstance(data, str):
            data = data.encode('utf-8')

        if not chunk_size:
            chunk_size = self.chunk_size

        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            yield cipher_context.update(chunk)

        # Ensure the finalization of the encryption
        yield cipher_context.finalize()

    def decrypt_b32(self, data_b32: str, iv: bytes=None) -> str:
        return self.decrypt(
            base64.b32decode(data_b32.encode('utf-8')), 
            iv
        ).decode('utf-8')

    @staticmethod
    def hash(data: str, iv: bytes=None):
        salt = iv.hex() if iv else ''
        return hashlib.sha256((data + salt).encode()).hexdigest()
    
    @staticmethod
    def generate_iv() -> bytes:
        return os.urandom(16)
