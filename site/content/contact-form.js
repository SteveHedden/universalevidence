const form = document.getElementById('contact-form');
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('contact-status');
  button.disabled = true; button.textContent = 'Sending…'; status.hidden = false; status.textContent = '';
  try {
    const fields = new FormData(form);
    const response = await fetch('/contact-submit', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fields)), signal: AbortSignal.timeout(20000)});
    const result = await response.json();
    status.textContent = result.message || 'Your message could not be sent. Please try again.';
    if (response.ok && result.ok) form.reset();
  } catch { status.textContent = 'We couldn’t confirm delivery. Please wait a moment before trying again.'; }
  finally {button.disabled = false; button.textContent = 'Send message ↗';}
});
