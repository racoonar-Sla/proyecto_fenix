
"""Preparación idempotente del laboratorio EC2/S3 de Proyecto Fénix."""

import hashlib
import os
import re

from botocore.exceptions import ClientError

from fenix.config.settings import client
from fenix.utils.errors import InventoryError, ValidationError
from fenix.utils.state import load, save


MANAGED_BY = "ppa2"


def _group_config() -> tuple[str, str, str]:
    """Obtiene la configuración del grupo."""
    group = os.getenv("GROUP_ID", "grupo01").strip() or "grupo01"
    project = os.getenv("PROJECT", "fenix").strip() or "fenix"
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1").strip() or "us-east-1"
    return group, project, region


def _expected_tags(group: str, project: str) -> dict:
    """Devuelve las etiquetas obligatorias del laboratorio."""
    return {
        "ManagedBy": MANAGED_BY,
        "Project": project,
        "GroupId": group,
        "Owner": group,
    }


def _aws_tags(tags: dict) -> list[dict]:
    """Convierte un diccionario al formato de etiquetas de AWS."""
    return [
        {"Key": key, "Value": value}
        for key, value in tags.items()
    ]


def _tags_dict(tags: list[dict] | None) -> dict:
    """Convierte etiquetas de AWS a un diccionario."""
    return {
        item["Key"]: item["Value"]
        for item in (tags or [])
    }


def _check_tag_ownership(current: dict, expected: dict, resource: str) -> None:
    """Evita adoptar recursos que tengan etiquetas de otro grupo."""
    for key, value in expected.items():
        existing = current.get(key)
        if existing is not None and existing != value:
            raise InventoryError(
                f"El recurso {resource} tiene una etiqueta "
                f"{key} incompatible con el grupo actual."
            )


def _ensure_ec2_tags(ec2, resource_id: str, expected: dict) -> None:
    """Completa las etiquetas EC2 sin sobrescribir valores ajenos."""
    response = ec2.describe_tags(
        Filters=[
            {"Name": "resource-id", "Values": [resource_id]}
        ]
    )
    current = _tags_dict(response.get("Tags", []))
    _check_tag_ownership(current, expected, resource_id)

    missing = {
        key: value
        for key, value in expected.items()
        if current.get(key) != value
    }

    if missing:
        ec2.create_tags(
            Resources=[resource_id],
            Tags=_aws_tags(missing),
        )


def _mock_inputs(ec2) -> tuple[str, str]:
    """Descubre una VPC y una AMI disponibles en el emulador."""
    vpcs = ec2.describe_vpcs().get("Vpcs", [])

    default_vpcs = [
        vpc for vpc in vpcs
        if vpc.get("IsDefault") is True
    ]

    if len(default_vpcs) == 1:
        vpc_id = default_vpcs[0]["VpcId"]
    elif len(vpcs) == 1:
        vpc_id = vpcs[0]["VpcId"]
    else:
        raise InventoryError(
            "No se pudo identificar una VPC única en LocalStack. "
            "Revisá el entorno preparado por la cátedra."
        )

    images = ec2.describe_images().get("Images", [])
    available = [
        image for image in images
        if image.get("State") in (None, "available")
        and image.get("ImageId")
    ]

    if not available:
        raise InventoryError(
            "LocalStack no devolvió una AMI mock disponible. "
            "No se creará una instancia con una AMI inventada."
        )

    available.sort(key=lambda image: image["ImageId"])
    image_id = available[0]["ImageId"]

    return vpc_id, image_id


def _security_group_name(group: str) -> str:
    """Genera un nombre estable para el grupo de seguridad."""
    safe_group = re.sub(r"[^a-zA-Z0-9-]", "-", group)
    return f"fenix-{safe_group[:180]}-sg"


def _ensure_security_group(
    ec2,
    existing: dict,
    group: str,
    project: str,
    vpc_id: str,
) -> str:
    """Recupera o crea el Security Group exclusivo del laboratorio."""
    expected = _expected_tags(group, project)
    group_id = existing.get("security_group_id")

    if group_id:
        try:
            response = ec2.describe_security_groups(
                GroupIds=[group_id]
            )
            groups = response.get("SecurityGroups", [])
            if groups:
                security_group = groups[0]
                if security_group.get("VpcId") != vpc_id:
                    raise InventoryError(
                        "El Security Group guardado pertenece a otra VPC."
                    )
                _ensure_ec2_tags(ec2, group_id, expected)
                return group_id

        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code not in ("InvalidGroup.NotFound",):
                raise

    name = _security_group_name(group)

    response = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    groups = response.get("SecurityGroups", [])

    if len(groups) > 1:
        raise InventoryError(
            "Se encontraron varios Security Groups con el mismo nombre."
        )

    if groups:
        group_id = groups[0]["GroupId"]
    else:
        response = ec2.create_security_group(
            GroupName=name,
            Description=f"Security Group exclusivo de Fénix para {group}",
            VpcId=vpc_id,
        )
        group_id = response["GroupId"]

    _ensure_ec2_tags(ec2, group_id, expected)
    return group_id


def _find_instance_by_tags(ec2, expected: dict) -> dict | None:
    """Busca una instancia existente perteneciente al grupo."""
    filters = [
        {
            "Name": f"tag:{key}",
            "Values": [value],
        }
        for key, value in expected.items()
    ]

    filters.append({
        "Name": "instance-state-name",
        "Values": ["pending", "running", "stopping", "stopped"],
    })

    response = ec2.describe_instances(Filters=filters)

    instances = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]

    if len(instances) > 1:
        raise InventoryError(
            "Hay varias instancias EC2 del grupo. "
            "No se elegirá una automáticamente."
        )

    return instances[0] if instances else None


def _ensure_ec2_node(
    ec2,
    existing: dict,
    group: str,
    project: str,
    vpc_id: str,
    image_id: str,
    security_group_id: str,
) -> str:
    """Recupera o crea la instancia EC2 sin duplicarla."""
    expected = _expected_tags(group, project)
    instance_id = existing.get("id") or existing.get("instance_id")

    if instance_id:
        try:
            response = ec2.describe_instances(
                InstanceIds=[instance_id]
            )
            instances = [
                instance
                for reservation in response.get("Reservations", [])
                for instance in reservation.get("Instances", [])
            ]

            if instances:
                instance = instances[0]
                if instance.get("State", {}).get("Name") == "terminated":
                    raise InventoryError(
                        "La instancia del inventario está terminada. "
                        "Se necesita reconciliar el inventario."
                    )
                _ensure_ec2_tags(ec2, instance_id, expected)
                return instance_id

        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code != "InvalidInstanceID.NotFound":
                raise

    instance = _find_instance_by_tags(ec2, expected)

    if instance:
        instance_id = instance["InstanceId"]
        _ensure_ec2_tags(ec2, instance_id, expected)
        return instance_id

    token_source = f"fenix-{project}-{group}-node-01"
    client_token = hashlib.sha256(
        token_source.encode("utf-8")
    ).hexdigest()[:64]

    response = ec2.run_instances(
        ImageId=image_id,
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1,
        ClientToken=client_token,
        SecurityGroupIds=[security_group_id],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": _aws_tags(expected),
            }
        ],
    )

    instances = response.get("Instances", [])
    if not instances:
        raise InventoryError(
            "EC2 no devolvió una instancia después de solicitar su creación."
        )

    return instances[0]["InstanceId"]


def _bucket_name(group: str, project: str) -> str:
    """Genera un nombre estable para el bucket."""
    safe = re.sub(
        r"[^a-z0-9.-]",
        "-",
        f"{project}-{group}-bucket-01".lower(),
    )
    return safe[:63].strip("-.")


def _bucket_exists(s3, bucket_name: str) -> bool:
    """Comprueba si existe el bucket sin ocultar errores de permisos."""
    try:
        s3.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")

        if code in ("404", "NoSuchBucket", "NotFound") or status == 404:
            return False

        raise


def _ensure_s3_bucket(
    s3,
    bucket_name: str,
    region: str,
    group: str,
    project: str,
) -> str:
    """Recupera o crea el bucket y completa sus etiquetas obligatorias."""
    expected = _expected_tags(group, project)

    if not _bucket_exists(s3, bucket_name):
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={
                    "LocationConstraint": region
                },
            )

    try:
        response = s3.get_bucket_tagging(Bucket=bucket_name)
        current = _tags_dict(response.get("TagSet", []))
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchTagSet", "NoSuchTagSetError"):
            current = {}
        else:
            raise

    _check_tag_ownership(current, expected, bucket_name)

    # Conserva etiquetas ajenas que no contradigan la identidad del grupo.
    merged = dict(current)
    merged.update(expected)

    s3.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={"TagSet": _aws_tags(merged)},
    )

    verification = s3.get_bucket_tagging(Bucket=bucket_name)
    verified = _tags_dict(verification.get("TagSet", []))

    for key, value in expected.items():
        if verified.get(key) != value:
            raise InventoryError(
                f"No se pudo verificar la etiqueta {key} del bucket."
            )

    return bucket_name


def setup_demo(inventory_path: str = "data/inventory.json") -> dict:
    """Prepara EC2 y S3 y guarda el inventario después de cada etapa."""
    group, project, region = _group_config()
    expected = _expected_tags(group, project)

    current = load(inventory_path)
    if not isinstance(current, dict):
        raise InventoryError("El inventario debe ser un objeto JSON.")

    ec2 = client("ec2")
    s3 = client("s3")

    node_meta = current.get("node-01", {})
    bucket_meta = current.get("bucket-01", {})

    if not isinstance(node_meta, dict) or not isinstance(bucket_meta, dict):
        raise InventoryError("Los registros del inventario no son válidos.")

    vpc_id, image_id = _mock_inputs(ec2)

    security_group_id = _ensure_security_group(
        ec2,
        node_meta,
        group,
        project,
        vpc_id,
    )

    # Guardar el Security Group antes de continuar con la instancia.
    current["node-01"] = {
        "type": "ec2",
        "id": node_meta.get("id") or node_meta.get("instance_id"),
        "instance_id": node_meta.get("id") or node_meta.get("instance_id"),
        "security_group_id": security_group_id,
        "expected_tags": _aws_tags(expected),
    }
    save(inventory_path, current)

    instance_id = _ensure_ec2_node(
        ec2,
        node_meta,
        group,
        project,
        vpc_id,
        image_id,
        security_group_id,
    )

    current["node-01"] = {
        "type": "ec2",
        "id": instance_id,
        "instance_id": instance_id,
        "security_group_id": security_group_id,
        "expected_tags": _aws_tags(expected),
    }
    save(inventory_path, current)

    bucket_name = (
        bucket_meta.get("id")
        or bucket_meta.get("bucket_name")
        or _bucket_name(group, project)
    )

    _ensure_s3_bucket(
        s3,
        bucket_name,
        region,
        group,
        project,
    )

    current["bucket-01"] = {
        "type": "s3",
        "id": bucket_name,
        "bucket_name": bucket_name,
        "expected_tags": _aws_tags(expected),
    }
    save(inventory_path, current)

    return current


def fault(
    resource_id: str,
    inventory_path: str = "data/inventory.json",
) -> None:
    """Provoca una falla controlada en un recurso registrado."""
    inventory = load(inventory_path)

    if not isinstance(inventory, dict):
        raise InventoryError("El inventario no es válido.")

    if resource_id not in ("node-01", "bucket-01"):
        raise ValidationError(
            "El recurso debe ser node-01 o bucket-01."
        )

    resource = inventory.get(resource_id)

    if not isinstance(resource, dict):
        raise InventoryError(
            f"El recurso {resource_id} no está registrado."
        )

    group, project, _ = _group_config()
    expected = _expected_tags(group, project)

    if resource_id == "node-01":
        instance_id = resource.get("id") or resource.get("instance_id")
        if not instance_id:
            raise InventoryError("El inventario no contiene InstanceId.")

        ec2 = client("ec2")
        response = ec2.describe_instances(
            InstanceIds=[instance_id]
        )

        instances = [
            instance
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]

        if not instances:
            raise InventoryError("No se encontró la instancia del inventario.")

        current_tags = _tags_dict(instances[0].get("Tags", []))
        _check_tag_ownership(current_tags, expected, instance_id)

        if any(current_tags.get(k) != v for k, v in expected.items()):
            raise InventoryError(
                "La instancia no tiene todas las etiquetas de propiedad."
            )

        ec2.stop_instances(InstanceIds=[instance_id])
        return

    bucket_name = resource.get("id") or resource.get("bucket_name")
    if not bucket_name:
        raise InventoryError("El inventario no contiene BucketName.")

    s3 = client("s3")

    try:
        response = s3.get_bucket_tagging(Bucket=bucket_name)
        current_tags = _tags_dict(response.get("TagSet", []))
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchTagSet", "NoSuchTagSetError"):
            current_tags = {}
        else:
            raise

    _check_tag_ownership(current_tags, expected, bucket_name)

    # La falla de demostración consiste en quitar Owner solamente.
    # Las demás etiquetas se conservan.
    current_tags.pop("Owner", None)

    s3.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={"TagSet": _aws_tags(current_tags)},
    )
