import { useEffect, useRef, type ReactNode } from 'react';

/** A modal on the native <dialog> element: the browser traps focus, makes the page inert,
 * closes on Escape and returns focus to the opener. Radix Dialog's scroll lock injects a style
 * element at runtime, which the strict CSP (no style-src 'unsafe-inline') blocks, so the
 * console does not use it (README, "Deviations"). */
export function Dialog({
  open,
  onClose,
  title,
  children,
  labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  labelledBy: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const handleClose = () => {
      onClose();
    };
    const handleCancel = (event: Event) => {
      event.preventDefault();
      onClose();
    };
    dialog.addEventListener('close', handleClose);
    dialog.addEventListener('cancel', handleCancel);
    return () => {
      dialog.removeEventListener('close', handleClose);
      dialog.removeEventListener('cancel', handleCancel);
    };
  }, [onClose]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={labelledBy}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded border border-ink bg-paper p-0 text-ink shadow-none"
    >
      <div className="rule-2 px-5 pt-3 pb-4">
        <h2 id={labelledBy} className="font-cond text-[20px] leading-7 font-semibold">
          {title}
        </h2>
        {children}
      </div>
    </dialog>
  );
}
