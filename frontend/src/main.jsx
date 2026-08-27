import React from 'react'
import { createRoot } from 'react-dom/client'
import App, { ErrorBoundary } from './App.jsx'
import './index.css'

// Last-resort boundary: a crash anywhere must render a reload card, never a
// silent black window (the desktop wrapper has no devtools to recover from).
createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
)
