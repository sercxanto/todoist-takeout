# Platform notes

What to know on macOS, on Windows and with network or synced folders. For installation and a quick start, see the [README](../README.md).

- **macOS certificates:** Python from python.org ships without CA certificates. If runs fail with `cannot verify the TLS certificate`, run *Install Certificates.command* from the Python folder in Applications, or point `SSL_CERT_FILE` at a CA bundle. The tool never turns certificate verification off.
- **Keep the target on a local disk.** The lock that prevents overlapping runs may not work on network file systems. If the target must live on one, make sure runs never overlap. Folders that a sync client such as OneDrive or Dropbox watches work, but a local directory that your regular file backup covers is the better choice.
- **Windows long paths:** file names in the backup stay short (an attachment file name has at most 100 characters), but a deeply nested target directory can exceed Windows' 260-character path limit. Use a short target path, or enable long path support (`LongPathsEnabled` in the registry or group policy).
