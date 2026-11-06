#!/usr/bin/env python3
"""
Rotina simples de backup de volumes EBS com Python e Boto3.
Criado para laboratório prático da AWS re/Start (Escola da Nuvem).

O que o script faz:
1. Conecta na AWS usando a biblioteca boto3.
2. Localiza todos os volumes EBS que possuem a tag: Backup = true.
3. Cria um snapshot para cada volume encontrado.
4. Mantém apenas os 2 backups mais recentes de cada volume, apagando os mais antigos.
"""

import boto3

# Conexão com o serviço EC2 da AWS na região desejada
ec2 = boto3.client('ec2', region_name='us-east-1')

# Quantidade de backups que desejamos manter armazenados
RETENCAO = 2

def fazer_backup_ebs():
    print("Iniciando rotina de backup dos volumes EBS...")

    # 1. Buscar volumes com a tag Backup = true
    volumes = ec2.describe_volumes(
        Filters=[
            {'Name': 'tag:Backup', 'Values': ['true', 'True']}
        ]
    )['Volumes']

    if not volumes:
        print("Nenhum volume encontrado com a tag Backup=true.")
        return

    for volume in volumes:
        vol_id = volume['VolumeId']
        print(f"\nProcessando volume: {vol_id}")

        # 2. Criar o snapshot do volume
        descricao = f"Backup automatico do volume {vol_id}"
        snapshot = ec2.create_snapshot(
            VolumeId=vol_id,
            Description=descricao,
            TagSpecifications=[
                {
                    'ResourceType': 'snapshot',
                    'Tags': [
                        {'Key': 'CreatedBy', 'Value': 'Script-Python-Backup'},
                        {'Key': 'VolumeId', 'Value': vol_id}
                    ]
                }
            ]
        )
        print(f"Snapshot criado com sucesso: {snapshot['SnapshotId']}")

        # 3. Aplicar política de retenção (manter os 2 mais recentes)
        snapshots = ec2.describe_snapshots(
            Filters=[
                {'Name': 'volume-id', 'Values': [vol_id]},
                {'Name': 'tag:CreatedBy', 'Values': ['Script-Python-Backup']}
            ]
        )['Snapshots']

        # Ordena os snapshots por data de criação (do mais recente para o mais antigo)
        snapshots_ordenados = sorted(snapshots, key=lambda x: x['StartTime'], reverse=True)

        # Se houver mais snapshots do que o limite de retenção, apaga os mais velhos
        if len(snapshots_ordenados) > RETENCAO:
            snapshots_para_apagar = snapshots_ordenados[RETENCAO:]
            for snap in snapshots_para_apagar:
                snap_id = snap['SnapshotId']
                print(f"Apagando snapshot antigo excedente: {snap_id}")
                ec2.delete_snapshot(SnapshotId=snap_id)

    print("\nRotina de backup finalizada com sucesso!")

if __name__ == '__main__':
    fazer_backup_ebs()
