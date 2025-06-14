package org.opengauss.migrationmcp;

import org.junit.jupiter.api.Test;
import org.opengauss.migrationmcp.entity.SourceDatabaseVo;
import org.opengauss.migrationmcp.service.DatabaseService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;

@SpringBootTest
public class DatabaseServiceTest {
    @Autowired
    DatabaseService databaseService;

    @Test
    void sourceDatabaseListTest() {
        List<SourceDatabaseVo> databaseClusters =
                databaseService.getSourceDatabaseList();

        databaseClusters.forEach(System.out::println);
    }

    @Test
    void targetDatabaseListTest() {
        databaseService.getTargetDatabaseList().forEach(System.out::println);
    }

    @Test
    void getSourceDatabaseNamesTest() {
        String ip = "192.168.0.118";
        String port = "3306";
        databaseService.getSourceDatabaseNameList(ip, port).forEach(System.out::println);
    }

    @Test
    void getTargetDatabaseNamesTest() {
        String ip = "192.168.2.59";
        String port = "5432";
        databaseService.getTargetDatabaseNameList(ip, port).forEach(System.out::println);
    }
}
