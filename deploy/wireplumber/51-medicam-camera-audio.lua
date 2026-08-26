table.insert(alsa_monitor.rules, {
  matches = {
    {
      {
        "device.name",
        "matches",
        "alsa_card.usb-4K_USB_Camera_4K_USB_Camera_01.00.00-*",
      },
    },
  },
  apply_properties = {
    ["device.disabled"] = true,
  },
})
