# Optional container operation — unqualified in this release build

Docker Desktop's Linux engine was unavailable during preparation. No image build, startup, writable-volume or restart test has passed for this candidate. Use the tested local-Python path first. The following configuration is prepared for separate validation; it is not a hosted deployment recipe.

The image uses UID 10001, one worker and port 8080. Both `/var/lib/civictrace/cases` and `/var/lib/civictrace/spend` are created writable for that user. Mount the SAME named volume across restarts:

```sh
docker build -t civictrace:portable-rc3 .
docker volume create civictrace-data
docker run --rm --name civictrace-local -p 127.0.0.1:8080:8080 -v civictrace-data:/var/lib/civictrace civictrace:portable-rc3
```

Open http://127.0.0.1:8080/workbench. Stop with Ctrl+C. Re-run the same command and volume to preserve cases and reservations. Removing the container does not remove the named volume; do not remove the volume to replenish allowance. If using a bind mount instead, prepare both subdirectories with write permission for UID 10001; a bind mount hides the image's directory ownership. Back up the entire volume while the app is stopped. One process per case store.

Live mode remains OFF in the image. Advanced operators may pass `CIVICTRACE_LIVE_ENABLED=1` and `NEBIUS_API_KEY` through their container secret/environment injection mechanism (by variable name, never a literal key in the command), plus explicitly reviewed lifetime limits. There is no hidden-key launcher inside the container. The prepared defaults are 2 investigations / $0.90 reservations, $0.15 per possible request, 3 requests per investigation, using `nvidia/nemotron-3-super-120b-a12b`. Preserve the volume and deployment ID. These are not provider billing guarantees. Do not expose this unauthenticated workbench publicly.
