# Test archives

* `plain.*`, `aes.zip`, `zipcrypto.zip`, `data-encrypted.7z`, `headers-encrypted.7z`, `single.txt.gz`:
  made with 7-Zip 26.05 from `docs/readme.txt`, `docs/notes/a.txt` and `big.txt` (see `tests/test_archive.py`).
  Password: `Secret1`.
* `traversal.zip` (an entry named `../evil.txt`) and `not-an-archive.zip`: made with Python.
* `rar*.rar`: from libarchive's test suite (https://github.com/libarchive/libarchive, BSD 2-clause,
  Copyright (c) Tim Kientzle and the libarchive contributors), decoded from the `.uu` files:
  `rar5-plain.rar` = test_read_format_rar5_multiple_files, `rar4-encrypted-names.rar` /
  `rar5-encrypted-names.rar` = test_read_format_rar{4,5}_encrypted_filenames (password `password`),
  `rar5-mixed-passwords.rar` = test_read_format_rar5_encrypted (d.txt uses `password2`).
  7-Zip can't create RAR files and RAR's own tool is proprietary, hence borrowed samples.
