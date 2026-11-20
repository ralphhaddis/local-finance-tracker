# Safari download routing

## Feasibility

Safari Web Extensions do not currently expose the WebExtensions `downloads` API, which is the API browsers use to choose a download path. A Safari extension therefore cannot directly set a per-website download folder.

Apple does support messaging between a Safari Web Extension and its containing macOS app. That makes a native companion possible, but it does not give the extension a Safari download-completed event or a destination override. A companion would have to infer which new file in `~/Downloads` belongs to a recent download action, then move it. That inference can be wrong for scripted downloads, concurrent downloads, duplicate filenames, or files created by another app. Moving files after download may also make Safari's Downloads list lose track of their location.

## Safe option for local-finance-tracker today

Set Safari's global download location to local-finance-tracker's existing inbox:

```text
/path/to/local-finance-tracker/inbox
```

local-finance-tracker already watches this folder, waits for downloads to finish, validates NatWest CSVs, and imports supported statements. This routes every Safari download there, so it is suitable only if that global behavior is acceptable.

## Per-site options

1. Use Safari's Save dialog when downloading a statement and select local-finance-tracker's inbox.
2. Use a third-party macOS file automation tool to sort completed downloads by stable filename or file type. This can route files, but it does not reliably know which webpage produced a file.
3. Build a native macOS helper that watches `~/Downloads` and applies configured website rules. Treat this as best-effort unless the workflow obtains the file through a browser with a supported download-destination API. Do not automatically move financial files based only on a loosely correlated click event.

## Decision before implementation

For reliable automatic per-site routing, the download itself needs to run in a component that can set its destination (for example, a dedicated macOS browser app using WebKit's download delegate). A Safari extension plus a file watcher is an approximation, not a dependable way to guarantee that NatWest statements always land in local-finance-tracker's inbox.
