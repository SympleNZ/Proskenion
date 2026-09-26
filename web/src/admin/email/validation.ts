/* Client-side mirror of `EmailConfigUpdate`'s field constraints (`proskenion/api/system.py`). */

export interface EmailFormValues {
  host: string;
  port: string;
  username: string;
  /** Blank means "leave the stored password unchanged" — never validated as required. */
  password: string;
  sender: string;
  recipient: string;
}

export interface EmailFormErrors {
  host?: string;
  port?: string;
  sender?: string;
  recipient?: string;
}

function looksLikeAddress(value: string): boolean {
  return value.includes("@") && value.trim().length >= 3;
}

export function validateEmailForm(values: EmailFormValues): EmailFormErrors {
  const errors: EmailFormErrors = {};

  if (!values.host.trim()) {
    errors.host = "Enter the SMTP host";
  }

  const port = Number(values.port);
  if (values.port.trim() === "" || !Number.isInteger(port) || port < 1 || port > 65535) {
    errors.port = "Enter a port between 1 and 65535";
  }

  if (!looksLikeAddress(values.sender)) {
    errors.sender = "Enter a sender address";
  }

  if (!looksLikeAddress(values.recipient)) {
    errors.recipient = "Enter a recipient address";
  }

  return errors;
}

export function isEmailFormValid(errors: EmailFormErrors): boolean {
  return Object.keys(errors).length === 0;
}
