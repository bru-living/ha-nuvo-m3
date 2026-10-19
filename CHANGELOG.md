# Changelog

## 0.9.7

- Add a visible Play all tracks entry to the indexed Tracks window, using the
  M3's native whole-library Play All command.
- Add a visible Play all album tracks entry to every indexed album window.

## 0.9.6

- Make Next and Previous explicitly select adjacent indexed global tracks when
  the M3 exposes a browser selection as a single-track playback list.
- Wrap from the final global track to the first, and vice versa.

## 0.9.5

- Tolerate isolated truncated rows returned by large M3 menus.
- Preserve absolute menu positions when skipping a damaged row so playback paths
  for all following tracks remain correct.

## 0.9.4

- Index the M3's top-level Tracks menu as a global playback context.
- Tracks-view playback now allows Next and Previous to cross artist boundaries.
- Associate artwork and artist metadata only where a track title is unambiguous.

## 0.9.3

- Added an indexed Tracks view alongside Artists.
- Sort tracks alphabetically and display their artist and album thumbnail.
- Page large indexed track libraries in groups of 100.

## 0.9.2

- Skip and report isolated artist or album menus that the M3 cannot open instead
  of discarding the entire library scan.
- Persist skipped-item counts with the completed index and expose them on the
  Library index sensor.
- Ensure reported catalogue progress never moves backwards during a scan.

## 0.9.1

- Increased menu-response timeouts during full-library indexing from 8 to 30 seconds.
- Added three attempts for transient scan-page timeouts, including slow paginated
  Artists responses following the M3's `PLEASEWAIT` notification.

## 0.9.0

- Added a persistent full-library index built from the M3 Artists menu.
- Added fast local artist, album and track browsing with album thumbnails.
- Added background scan/cancel controls, progress, counts and diagnostics.
- Indexed playback retains the M3's numeric menu paths and does not browse live until an item is played.

## 0.8.3

- Fixed Juke Box Mode toggling by using the M3 protocol's MENUPLAY command.
- Re-reads the option after toggling so the Home Assistant switch reflects the confirmed M3 state.

## 0.8.2

- Corrected the integration manifest ordering required by Hassfest.
- Corrected the HACS repository manifest.
- Granted GitHub Actions read access so validation can check out the repository.

## 0.8.1

- Added local NuVo/Bru Living Home Assistant brand icons and logos.
- Added per-output Juke Box Mode switches through the dynamic M3 Options menu.
- Added bounded native menu keepalive and automatic session release.
- Added optional MusicBrainz and Cover Art Archive enrichment with canonical-release ranking.
- Added persistent Home Assistant-managed playlists.
- Added native M3 media browsing with paging and deterministic path reconstruction.
- Added TCP and local serial configuration, reconfiguration, diagnostics and reconnect handling.
- Added three output media players and server/firmware sensors.
