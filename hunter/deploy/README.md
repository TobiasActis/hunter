# Desplegar HUNTER en un servidor gratis 24/7

Objetivo: que `main.py` corra sin parar en una VM gratis (Oracle Cloud
Always Free -- la única opción "gratis de verdad", no de prueba, que
no se "duerme" como Render/Railway free tier -- ver README principal
para la comparación completa).

## Parte 1 -- esto lo tenés que hacer VOS (no puedo crear la cuenta por vos)

1. Andá a **cloud.oracle.com/free** y creá una cuenta. Pide email,
   teléfono y una tarjeta para verificar identidad -- **no te cobra
   nada** mientras te quedes en los recursos "Always Free", pero el
   paso de verificación es inevitable, así lo diseñó Oracle.
2. Una vez adentro de la consola, creá una **Compute Instance**:
   - Shape: elegí **Ampere A1.Flex** (ARM, hasta 4 OCPU / 24GB gratis
     -- con 2 OCPU / 12GB alcanza de sobra). Si tu región no tiene
     capacidad ARM disponible (pasa seguido), usá el shape alternativo
     **VM.Standard.E2.1.Micro** (x86, 1GB RAM -- más justo pero
     funciona).
   - Imagen: **Ubuntu 22.04** (o la LTS más nueva que ofrezcan).
   - En "Add SSH keys" subí tu clave pública (o generá un par nuevo
     ahí mismo y guardate la privada).
   - Dejá el resto en default (red "Always Free-eligible").
3. Cuando la instancia esté "Running", copiá la **IP pública** que
   te muestra la consola.
4. Confirmá que podés entrar por SSH vos mismo:
   ```bash
   ssh ubuntu@TU_IP_PUBLICA
   ```
   (el usuario default en las imágenes Ubuntu de Oracle es `ubuntu`).

## Parte 2 -- desde acá puedo ayudarte con el resto

Una vez que tengas la IP y confirmes que el SSH funciona, pasame la
IP (y avisame si el usuario no es `ubuntu`) y puedo:
- Copiar el proyecto a la VM (`scp`/`rsync`).
- Correr `deploy/setup.sh` ahí (instala Python, crea el entorno
  virtual, instala dependencias, y deja `main.py` corriendo como
  servicio systemd que se reinicia solo si se cae).
- Confirmar que quedó corriendo y mostrarte los logs.

O si preferís hacerlo vos mismo paso a paso:

```bash
# Desde tu PC, copiar el proyecto a la VM (ajustá la ruta):
scp -r "D:\Descargas\hunter-project\hunter" ubuntu@TU_IP_PUBLICA:~/hunter

# Ya en la VM:
ssh ubuntu@TU_IP_PUBLICA
cd ~/hunter
chmod +x deploy/setup.sh
./deploy/setup.sh
```

Al final el script te muestra los comandos para ver logs, reiniciar,
y cómo abrir el dashboard.

## Ver el dashboard desde tu PC (sin exponerlo a internet)

El dashboard escucha en `127.0.0.1:8000` DENTRO de la VM a propósito
-- no tiene login, así que exponerlo al puerto público de internet
significaría que cualquiera podría tocar los botones de "Comprar"/
"Cerrar" (paper trading, sin plata real, pero igual es tu data).
En vez de abrir el firewall, usá un túnel SSH:

```bash
ssh -L 8000:localhost:8000 ubuntu@TU_IP_PUBLICA
```

Dejá esa terminal abierta y entrá a `http://localhost:8000` en tu
navegador -- es como si el dashboard corriera en tu PC.

## Qué pasa con los datos ya acumulados (185 alertas, 187 posiciones)

`data/hunter.db` viaja con el `scp`/`rsync` de la Parte 2 -- si
copiás la carpeta completa del proyecto, todo lo que ya juntaste hoy
se sube con vos, no arranca de cero.

## Sobre el límite de Oracle por inactividad

Oracle puede reclamar una instancia "Always Free" si el uso de CPU
queda por debajo del 20% (percentil 95) durante 7 días seguidos. Para
tu pedido de "mínimo 24hs" esto no aplica (la ventana que mide Oracle
es de 7 días). Para uso sostenido más largo, el timer de
`hunter-brain.py` que corre una vez por día ya genera uso real de CPU
(scikit-learn entrenando) -- no es un truco para "hacerse el ocupado",
es directamente lo que pediste ("que aprenda solo"), y de paso ayuda
a que la instancia no luzca inactiva.
