import Cocoa
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                      kCGNullWindowID) as? [[String: Any]] ?? []
for w in list {
  let owner = w[kCGWindowOwnerName as String] as? String ?? "?"
  let num = w[kCGWindowNumber as String] ?? "?"
  let bounds = w[kCGWindowBounds as String] ?? "?"
  print(owner, num, bounds)
}
