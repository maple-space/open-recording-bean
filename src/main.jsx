import React from 'react'
import ReactDOM from 'react-dom/client'
import PortalApp from './platform/PortalApp.jsx'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <PortalApp />
  </React.StrictMode>,
)

// The old standalone app cached every GET. A local workspace must never display
// cached device/health responses as if the hardware service were still online.
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistrations().then((registrations) => {
    for (const registration of registrations) {
      const worker = registration.active || registration.waiting || registration.installing
      if (worker && new URL(worker.scriptURL).origin === window.location.origin && new URL(worker.scriptURL).pathname === '/sw.js') registration.unregister()
    }
  }).catch(() => {})
}
