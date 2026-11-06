#!/usr/bin/env python3
"""
==============================================================================
Script: backup_ebs.py
Finalidade: Automação de criação de snapshots para volumes EBS anexados a
            instâncias EC2 com tags específicas e expurgo de snapshots antigos
            com base em política de retenção.
Autor: Cesar Lana (Laboratório AWS)
==============================================================================
"""

import argparse
import datetime
import logging
import os
import sys
from typing import Any, Dict, List

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    print(
        "[FATAL] Boto3 não encontrado. Instale com: pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)


def setup_logger(log_file: str | None = None, verbose: bool = False) -> logging.Logger:
    """Configura o logger com formatação consistente para stdout e arquivo opcional."""
    logger = logging.getLogger("ebs_backup")
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="[%(levelname)s] %(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning("Não foi possível criar o arquivo de log '%s': %s", log_file, e)

    return logger


def parse_arguments() -> argparse.Namespace:
    """Configura e valida os argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        description="Automação de backup e retenção de snapshots EBS via Boto3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--tag-key",
        default="Backup",
        help="Chave da tag utilizada para identificar instâncias elegíveis",
    )
    parser.add_argument(
        "--tag-value",
        default="True",
        help="Valor da tag utilizada para identificar instâncias elegíveis",
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=2,
        help="Quantidade máxima de snapshots mais recentes a serem retidos por volume",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        help="Região AWS a ser utilizada",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula a execução sem criar ou remover snapshots reais na AWS",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Caminho para gravação de arquivo de log (ex: /var/log/ebs-backup.log)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Ativa modo detalhado com mensagens de DEBUG",
    )
    return parser.parse_args()


def get_target_instances(
    ec2_client: Any, tag_key: str, tag_value: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Busca instâncias ativas que contenham a tag especificada."""
    logger.info(
        "Buscando instâncias EC2 com tag '%s=%s'...",
        tag_key,
        tag_value,
    )
    filters = [
        {"Name": f"tag:{tag_key}", "Values": [tag_value]},
        {"Name": "instance-state-name", "Values": ["running", "stopped"]},
    ]

    try:
        paginator = ec2_client.get_paginator("describe_instances")
        instances: List[Dict[str, Any]] = []

        for page in paginator.paginate(Filters=filters):
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instances.append(instance)

        logger.info("Total de instâncias elegíveis encontradas: %d", len(instances))
        return instances
    except (BotoCoreError, ClientError) as e:
        logger.error("Erro ao listar instâncias EC2: %s", e)
        raise


def create_volume_snapshot(
    ec2_client: Any,
    instance_id: str,
    device_name: str,
    volume_id: str,
    retention: int,
    dry_run: bool,
    logger: logging.Logger,
) -> str | None:
    """Cria snapshot do volume EBS com tags de identificação e governança."""
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    description = (
        f"Automated backup of {volume_id} ({device_name}) from {instance_id} at {timestamp_str}"
    )

    tags = [
        {"Key": "Name", "Value": f"Backup-{instance_id}-{device_name}-{timestamp_str}"},
        {"Key": "CreatedBy", "Value": "EBS-Backup-Automation"},
        {"Key": "InstanceId", "Value": instance_id},
        {"Key": "VolumeId", "Value": volume_id},
        {"Key": "DeviceName", "Value": device_name},
        {"Key": "RetentionPolicy", "Value": str(retention)},
        {"Key": "CreatedAt", "Value": now.isoformat()},
    ]

    logger.info(
        "Criando snapshot para o volume %s (%s) da instância %s...",
        volume_id,
        device_name,
        instance_id,
    )

    if dry_run:
        logger.info(
            "[DRY-RUN] Simulação: Snapshot seria criado para o volume %s com tags: %s",
            volume_id,
            tags,
        )
        return "snap-simulated-dry-run"

    try:
        response = ec2_client.create_snapshot(
            VolumeId=volume_id,
            Description=description,
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": tags,
                }
            ],
        )
        snapshot_id = response.get("SnapshotId")
        logger.info("Snapshot criado com sucesso: %s", snapshot_id)
        return snapshot_id
    except ClientError as e:
        logger.error("Falha ao criar snapshot para o volume %s: %s", volume_id, e)
        return None


def apply_retention_policy(
    ec2_client: Any,
    volume_id: str,
    retention_count: int,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """
    Remove snapshots antigos criados pela automação que excedem o limite de retenção.
    Retém os 'retention_count' mais recentes ordenados por StartTime.
    """
    logger.info(
        "Aplicando política de retenção para o volume %s (Manter: %d mais recentes)...",
        volume_id,
        retention_count,
    )

    filters = [
        {"Name": "volume-id", "Values": [volume_id]},
        {"Name": "tag:CreatedBy", "Values": ["EBS-Backup-Automation"]},
        {"Name": "status", "Values": ["completed", "pending"]},
    ]

    try:
        response = ec2_client.describe_snapshots(
            OwnerIds=["self"],
            Filters=filters,
        )
        snapshots = response.get("Snapshots", [])

        # Ordena do mais recente para o mais antigo com base em StartTime
        snapshots.sort(key=lambda x: x["StartTime"], reverse=True)

        total_snapshots = len(snapshots)
        logger.info(
            "Volume %s possui %d snapshots gerenciados pela automação.",
            volume_id,
            total_snapshots,
        )

        deleted_count = 0
        if total_snapshots > retention_count:
            snapshots_to_delete = snapshots[retention_count:]
            logger.info(
                "Identificados %d snapshots além do limite de retenção para exclusão.",
                len(snapshots_to_delete),
            )

            for snap in snapshots_to_delete:
                snap_id = snap["SnapshotId"]
                start_time = snap["StartTime"].isoformat()

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Simulação: Excluiria snapshot %s criado em %s",
                        snap_id,
                        start_time,
                    )
                    deleted_count += 1
                else:
                    try:
                        logger.info("Excluindo snapshot antigo: %s (%s)...", snap_id, start_time)
                        ec2_client.delete_snapshot(SnapshotId=snap_id)
                        logger.info("Snapshot %s excluído com sucesso.", snap_id)
                        deleted_count += 1
                    except ClientError as e:
                        logger.error("Erro ao excluir snapshot %s: %s", snap_id, e)
        else:
            logger.info(
                "Nenhum snapshot excedente para o volume %s. (Existentes: %d <= Retenção: %d)",
                volume_id,
                total_snapshots,
                retention_count,
            )

        return deleted_count
    except (BotoCoreError, ClientError) as e:
        logger.error(
            "Erro ao aplicar retenção de snapshots no volume %s: %s", volume_id, e
        )
        return 0


def main() -> int:
    args = parse_arguments()
    logger = setup_logger(log_file=args.log_file, verbose=args.verbose)

    logger.info("==========================================================")
    logger.info("Iniciando rotina de automação de backup EBS")
    logger.info("Região AWS       : %s", args.region)
    logger.info("Filtro de Tag    : %s=%s", args.tag_key, args.tag_value)
    logger.info("Limite Retenção  : %d snapshots", args.retention)
    logger.info("Modo Dry-Run     : %s", args.dry_run)
    logger.info("==========================================================")

    if args.retention < 1:
        logger.error("A política de retenção deve ser de no mínimo 1 snapshot.")
        return 1

    try:
        session = boto3.Session(region_name=args.region)
        ec2_client = session.client("ec2")
    except Exception as e:
        logger.error("Falha ao inicializar o cliente Boto3 da AWS: %s", e)
        return 1

    try:
        instances = get_target_instances(
            ec2_client, args.tag_key, args.tag_value, logger
        )
    except Exception:
        return 1

    if not instances:
        logger.warning(
            "Nenhuma instância encontrada com a tag '%s=%s'. Encerrando.",
            args.tag_key,
            args.tag_value,
        )
        return 0

    total_snapshots_created = 0
    total_snapshots_purged = 0

    for instance in instances:
        instance_id = instance.get("InstanceId", "unknown")
        block_mappings = instance.get("BlockDeviceMappings", [])

        logger.info(
            "Processando Instância: %s (Total de volumes anexados: %d)",
            instance_id,
            len(block_mappings),
        )

        for mapping in block_mappings:
            device_name = mapping.get("DeviceName", "unknown")
            ebs = mapping.get("Ebs")

            if not ebs:
                logger.warning(
                    "Mapeamento de bloco %s na instância %s não é EBS. Ignorando.",
                    device_name,
                    instance_id,
                )
                continue

            volume_id = ebs.get("VolumeId")
            if not volume_id:
                continue

            # 1. Criação do snapshot do volume
            snap_id = create_volume_snapshot(
                ec2_client=ec2_client,
                instance_id=instance_id,
                device_name=device_name,
                volume_id=volume_id,
                retention=args.retention,
                dry_run=args.dry_run,
                logger=logger,
            )
            if snap_id:
                total_snapshots_created += 1

            # 2. Aplicação da política de retenção e expurgo
            purged = apply_retention_policy(
                ec2_client=ec2_client,
                volume_id=volume_id,
                retention_count=args.retention,
                dry_run=args.dry_run,
                logger=logger,
            )
            total_snapshots_purged += purged

    logger.info("==========================================================")
    logger.info("Resumo da Execução:")
    logger.info("Snapshots criados   : %d", total_snapshots_created)
    logger.info("Snapshots expurgados: %d", total_snapshots_purged)
    logger.info("Rotina finalizada com sucesso!")
    logger.info("==========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
