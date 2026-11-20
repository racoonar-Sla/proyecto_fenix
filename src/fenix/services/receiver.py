from fastapi import FastAPI, Request, HTTPException, Header
import json
import os

from fenix.utils.crypto import verify
from fenix.utils.errors import ValidationError
from fenix.services.events import validate

# Intentamos importar la función de SQS de varias formas posibles por si Mauricio (I2) le puso otro nombre
try:
    from fenix.services.sqs import send_message
except ImportError:
    try:
        from fenix.services.sqs import enqueue_event
    except ImportError:
        try:
            from fenix.services.sqs import send_sqs_message
        except ImportError:
            send_message = None

app = FastAPI(title="Proyecto Fénix - Webhook Receptor")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secreto_de_prueba_de_16_chars_minimo")

@app.post("/webhook", status_code=202)
async def receive_webhook(request: Request, x_webhook_signature: str = Header(None)):
    raw_body = await request.body()

    if not x_webhook_signature:
        raise HTTPException(status_code=401, detail="Falta la cabecera X-Webhook-Signature")
    
    try:
        verify(WEBHOOK_SECRET, x_webhook_signature, raw_body)
    except ValidationError as e:
        raise HTTPException(status_code=401, detail=str(e))

    try:
        payload = json.loads(raw_body.decode('utf-8'))
        validated_event = validate(payload) 
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON malformado")
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error de validación: {str(e)}")

    if send_message is None:
        raise HTTPException(status_code=500, detail="Servicio SQS no implementado aún o cambió de nombre")
    
    try:
        # Aca enviamos el evento a SQS
        send_message(json.dumps(validated_event))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Error de red al encolar: {str(e)}")

    return {"status": "accepted", "message": "Alerta encolada correctamente"}